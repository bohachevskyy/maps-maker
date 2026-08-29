"""Statistics: values keyed by a join key, and nothing else.

A provider knows nothing about polygons. It answers "what is this variable, for
these keys" and hands back a mapping. Cartography supplies the keys; `harvest`
performs the join.

Keys are ISO3 at country level and ISO 3166-2 below it -- the codes both Natural
Earth and Overture already carry.

Registering a source
--------------------
A source declares what it *can do*, not what it holds. Freezing a list of
indicators into the registry was the earlier design and it was wrong: OWID
publishes thousands of charts, and a hand-picked ten refused "unemployment"
while the catalogue had 98 matching charts.

Write `mapsvc/statistics/<name>.py` with a `Capabilities` and the functions it
advertises, then add the module name to `_MODULES` below.

    CAPABILITIES = Capabilities(
        source="owid", levels=("admin_0",), key="ISO3",
        license="CC BY 4.0", attribution="Our World in Data (CC BY 4.0)",
        searchable=True,                    # implement search() + describe()
        description="thousands of charts on health, environment, energy...",
    )

    def search(query, limit) -> list[Candidate]   # searchable sources only
    def describe(variable_id) -> Variable         # searchable sources only
    def variables(level) -> dict[str, Variable]   # fixed sources only
    def load(variable_id, level, region) -> Values

A **fixed** source (natural_earth) enumerates a small, stable set of columns and
the agent picks from an enum. A **searchable** source (owid) is too large to
enumerate: the agent emits a query, the service searches, and the agent picks
from what came back -- so it still cannot invent an id, the constraint is just
computed per request instead of frozen.

`Variable.level` is the measurement level and it is load-bearing: "count" treats
0 as no-data (a country with zero people is a placeholder row), "ratio" does not
(0% unemployment is a real observation).
"""

import functools
from dataclasses import dataclass, field

# The registered sources, in the order they are offered.
_MODULES = ("natural_earth", "owid")


@dataclass(frozen=True)
class Capabilities:
    """What a source can do. Declared once per provider module."""

    source: str
    levels: tuple
    key: str                      # "ISO3" at admin_0, "ISO3166_2" below
    license: str
    attribution: str
    description: str
    searchable: bool = False


@dataclass(frozen=True)
class Variable:
    """One measurable thing, however the source describes it."""

    id: str
    label: str
    unit: str | None
    level: str                    # count | ratio | ordinal | nominal
    year_col: str | None = None
    timespan: str | None = None
    citation: str | None = None


@dataclass(frozen=True)
class Candidate:
    """A search hit, offered to the agent to choose from."""

    id: str
    title: str
    subtitle: str = ""
    coverage: int = 0             # entities the source has data for

    def as_prompt_line(self) -> str:
        note = f" — {self.subtitle}" if self.subtitle else ""
        return f"{self.id}: {self.title}{note} ({self.coverage} entities)"


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


def capabilities(source: str) -> Capabilities:
    return provider(source).CAPABILITIES


def all_capabilities() -> list:
    return [m.CAPABILITIES for m in _providers().values()]


def variables_for(level: str, source: str) -> dict:
    """What a *fixed* source publishes at a level. Empty for searchable ones."""
    module = provider(source)
    if module.CAPABILITIES.searchable:
        return {}
    return module.VARIABLES.get(level, {})


def search(source: str, query: str, limit: int = 8) -> list:
    """Ask a searchable source for candidates matching a natural-language query."""
    module = provider(source)
    if not module.CAPABILITIES.searchable:
        raise StatisticsError(
            f"{source!r} is a fixed source; its variables are listed, not searched",
            "variable.source",
        )
    return module.search(query, limit)


def describe(source: str, variable_id: str) -> Variable:
    """Resolve a variable id to its measurement metadata.

    For a fixed source this is a dict lookup; for a searchable one it is a
    metadata fetch, because there is no local catalogue to consult.
    """
    module = provider(source)
    if module.CAPABILITIES.searchable:
        return module.describe(variable_id)
    for level_ids in module.VARIABLES.values():
        if variable_id in level_ids:
            return level_ids[variable_id]
    raise StatisticsError(
        f"{source!r} has no variable {variable_id!r}", "variable.id"
    )


def sources_for(variable_id: str) -> list[str]:
    """Which *fixed* sources publish this id. Searchable ones cannot answer."""
    return [
        name for name, module in _providers().items()
        if not module.CAPABILITIES.searchable
        and any(variable_id in ids for ids in module.VARIABLES.values())
    ]


def level_of(variable_id: str) -> str | None:
    """The admin level a fixed-source variable belongs to."""
    for module in _providers().values():
        if module.CAPABILITIES.searchable:
            continue
        for level, ids in module.VARIABLES.items():
            if variable_id in ids:
                return level
    return None


@functools.lru_cache(maxsize=1)
def fixed_variables() -> dict:
    """Every variable a fixed source offers -- the agent's enum comes from here."""
    merged: dict = {}
    for module in _providers().values():
        if not module.CAPABILITIES.searchable:
            for ids in module.VARIABLES.values():
                merged.update(ids)
    return merged
