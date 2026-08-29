"""Natural Earth's attribute columns, served as statistics.

Natural Earth ships geometry and attributes in one file. This module reads only
the attributes and returns them keyed by ISO code, so the values can be joined
onto any provider's polygons -- Overture's included.
"""

from mapsvc import registry
from mapsvc.statistics import StatisticsError, Values

SOURCE = "natural_earth"
LICENSE = "public domain"
ATTRIBUTION = "Natural Earth"

# The join key each level's rows are addressed by.
KEY_PROPERTY = {"admin_0": "ADM0_A3", "admin_1": "iso_3166_2"}

# Of the 168 properties on each country, these six are the mappable ones.
COUNTRY_VARIABLES = {
    "POP_EST":    {"level": "count",   "unit": "people",      "year_col": "POP_YEAR"},
    "GDP_MD":     {"level": "count",   "unit": "million USD", "year_col": "GDP_YEAR"},
    "POP_RANK":   {"level": "ordinal", "unit": None,          "year_col": None},
    "INCOME_GRP": {"level": "ordinal", "unit": None,          "year_col": None},
    "ECONOMY":    {"level": "ordinal", "unit": None,          "year_col": None},
    "SUBREGION":  {"level": "nominal", "unit": None,          "year_col": None},
}

# Admin-1 carries 121 properties per unit and not one of them is population,
# GDP or income; `area_sqkm` is 0 for every unit on earth. What is left is
# classification and cartographic prominence -- enough to distinguish the kind
# of a unit, and nothing that could be mistaken for a statistic.
UNIT_VARIABLES = {
    "type":      {"level": "nominal", "unit": None, "year_col": None},
    "type_en":   {"level": "nominal", "unit": None, "year_col": None},
    "region":    {"level": "nominal", "unit": None, "year_col": None},
    "labelrank": {"level": "ordinal", "unit": None, "year_col": None},
}

# The catalogue this source publishes, by level. `statistics` reads this to
# build its lookups, so registering a source is declaring one dict.
VARIABLES = {"admin_0": COUNTRY_VARIABLES, "admin_1": UNIT_VARIABLES}


def variables(level: str) -> dict:
    return VARIABLES.get(level, {})


def load(variable_id: str, level: str, region: str) -> Values:
    from mapsvc.harvest import load_source, select_region

    available = variables(level)
    if variable_id not in available:
        from mapsvc import statistics

        belongs = statistics.level_of(variable_id)
        detail = (f"{variable_id!r} is an {belongs} variable" if belongs
                  else f"unknown variable {variable_id!r}")
        raise StatisticsError(
            f"{detail}; natural_earth offers "
            f"{', '.join(sorted(available)) or 'nothing'} at {level}", "variable.id"
        )

    meta = available[variable_id]
    key_property = KEY_PROPERTY[level]
    features = select_region(load_source(level)["features"], region, level)

    values: dict = {}
    years: set = set()
    for feature in features:
        props = feature["properties"]
        key = props.get(key_property)
        if not key:
            continue
        values[str(key).upper()] = props.get(variable_id)
        if meta["year_col"] and isinstance(props.get(meta["year_col"]), int):
            years.add(props[meta["year_col"]])

    return Values(values=values, provenance={
        "source": registry.SOURCE_NAME,
        "vintage": registry.SOURCE_VINTAGE,
        "variable": variable_id,
        "unit": meta["unit"],
        "level": meta["level"],
        "year": _year_label(years),
        "key": key_property,
    })


def _year_label(years: set) -> str | None:
    if not years:
        return None
    if len(years) == 1:
        return str(next(iter(years)))
    return f"{min(years)}–{max(years)}"
