"""Natural Earth's attribute columns, served as statistics.

Natural Earth ships geometry and attributes in one file. This module reads only
the attributes and returns them keyed by ISO code, so the values can be joined
onto any provider's polygons -- Overture's included.
"""

from mapsvc import registry
from mapsvc.statistics import StatisticsError, Values

# The join key each level's rows are addressed by.
KEY_PROPERTY = {"admin_0": "ADM0_A3", "admin_1": "iso_3166_2"}


def variables(level: str) -> dict:
    return registry.variables_for(level) if level in KEY_PROPERTY else {}


def load(variable_id: str, level: str, region: str) -> Values:
    from mapsvc.harvest import load_source, select_region

    available = variables(level)
    if variable_id not in available:
        belongs = registry.level_of(variable_id)
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
