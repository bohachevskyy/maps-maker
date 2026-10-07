"""Natural Earth's attribute columns, served as statistics.

Natural Earth ships geometry and attributes in one file. This module reads only
the attributes and returns them keyed by ISO code, so the values can be joined
onto any provider's polygons -- Overture's included.
"""

from mapsvc import registry
from mapsvc.statistics import Capabilities, StatisticsError, Values, Variable

SOURCE = "natural_earth"
CAPABILITIES = Capabilities(
    source=SOURCE,
    levels=("admin_0", "admin_1"),
    key={"admin_0": "ISO3", "admin_1": "ISO3166_2"},
    license="public domain",
    attribution="Natural Earth",
    searchable=False,
    description=(
        "The columns that ship with the boundary file: population, GDP, income "
        "group, economy and subregion by country, plus unit type and label rank "
        "for sub-national units. A small fixed set, instantly available."
    ),
)
LICENSE = CAPABILITIES.license
ATTRIBUTION = CAPABILITIES.attribution

# The join key each level's rows are addressed by.
KEY_PROPERTY = {"admin_0": "ISO_A3, else ADM0_A3", "admin_1": "iso_3166_2"}

# Of the 168 properties on each country, these six are the mappable ones.
def _v(vid, level, unit=None, year_col=None, label=None):
    return Variable(id=vid, label=label or vid, unit=unit, level=level,
                    year_col=year_col)


# Natural Earth stores GDP in millions of US$. Like EIA's "thousand" units it is
# converted at load, so a derived ratio reads "US$ per person" at its true size
# instead of 0.11 "million USD per person".
_SCALE = {"GDP_MD": 1_000_000}
# Placeholders that must survive untouched for harvest to recognise them.
NO_DATA_MARKERS = (-99, 0)

COUNTRY_VARIABLES = {
    "POP_EST":    _v("POP_EST",    "count",   "people",      "POP_YEAR", "population"),
    "GDP_MD":     _v("GDP_MD",     "count",   "US$",         "GDP_YEAR", "GDP"),
    "POP_RANK":   _v("POP_RANK",   "ordinal", None, None, "population rank"),
    "INCOME_GRP": _v("INCOME_GRP", "ordinal", None, None, "income group"),
    "ECONOMY":    _v("ECONOMY",    "ordinal", None, None, "economy type"),
    "SUBREGION":  _v("SUBREGION",  "nominal", None, None, "subregion"),
}

# Admin-1 carries 121 properties per unit and not one of them is population,
# GDP or income; `area_sqkm` is 0 for every unit on earth. What is left is
# classification and cartographic prominence -- enough to distinguish the kind
# of a unit, and nothing that could be mistaken for a statistic.
UNIT_VARIABLES = {
    "type":      _v("type",      "nominal", None, None, "unit type (native)"),
    "type_en":   _v("type_en",   "nominal", None, None, "unit type (English)"),
    "region":    _v("region",    "nominal", None, None, "sub-national grouping"),
    "labelrank": _v("labelrank", "ordinal", None, None, "cartographic prominence"),
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
    keyed_years: dict = {}
    for feature in features:
        props = feature["properties"]
        key = (registry.iso3_of(props) if level == "admin_0"
               else props.get(key_property))
        if not key:
            continue
        value = props.get(variable_id)
        scale = _SCALE.get(variable_id)
        if scale and isinstance(value, (int, float)) and value not in NO_DATA_MARKERS:
            value = value * scale
        values[str(key).upper()] = value
        if meta.year_col and isinstance(props.get(meta.year_col), int):
            keyed_years[str(key).upper()] = props[meta.year_col]
    years = set(keyed_years.values())

    return Values(values=values, years=keyed_years, provenance={
        "source": registry.SOURCE_NAME,
        "vintage": registry.SOURCE_VINTAGE,
        "variable": meta.label,
        "unit": meta.unit,
        "level": meta.level,
        "license": CAPABILITIES.license,
        "attribution": CAPABILITIES.attribution,
        "year": _year_label(years),
        "key": key_property,
    })


def _year_label(years: set) -> str | None:
    if not years:
        return None
    if len(years) == 1:
        return str(next(iter(years)))
    return f"{min(years)}–{max(years)}"
