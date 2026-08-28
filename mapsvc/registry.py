"""Static tables: what can be mapped, at which level, and where it comes from."""

# Geometry scale for country maps. Swap to "10m" for sharper coastlines.
SCALE = "50m"

# Sub-national units get their own scale, and it is not negotiable: the 50m
# admin-1 file carries only 294 units across nine large countries (RUS, USA,
# IND, IDN, CHN, BRA, CAN, AUS, ZAF). Ukraine's oblasts exist only at 10m.
ADMIN1_SCALE = "10m"

_BASE = ("https://raw.githubusercontent.com/nvkelso/natural-earth-vector/"
         "master/geojson/ne_{scale}_{dataset}.geojson")

SOURCE_NAME = "Natural Earth"
SOURCE_VINTAGE = "v5.1.1"

# Canonical levels. Each cartography provider maps these onto its own
# vocabulary; not every provider offers every level.
LEVELS = ("admin_0", "admin_1", "admin_2", "admin_3")
BASEMAPS = ("natural_earth", "overture")
# Overture by default: far more detail, and the only source below admin_1. It
# is queried live, so the harvest cache does the heavy lifting -- see harvest.py.
DEFAULT_BASEMAP = "overture"
DETAILS = ("simplified", "full")
METHODS = ("quantile", "equal_interval", "jenks")
PROJECTIONS = ("auto", "albers", "mercator", "mollweide")
MISSING_MODES = ("hatch", "grey", "exclude")
SOURCES = ("natural_earth", "owid")

K_MIN, K_MAX = 3, 9

# Of the 168 properties on each country, these six are the mappable ones.
VARIABLES = {
    "POP_EST":    {"level": "count",   "unit": "people",      "year_col": "POP_YEAR"},
    "GDP_MD":     {"level": "count",   "unit": "million USD", "year_col": "GDP_YEAR"},
    "POP_RANK":   {"level": "ordinal", "unit": None,          "year_col": None},
    "INCOME_GRP": {"level": "ordinal", "unit": None,          "year_col": None},
    "ECONOMY":    {"level": "ordinal", "unit": None,          "year_col": None},
    "SUBREGION":  {"level": "nominal", "unit": None,          "year_col": None},
}

# Admin-1 carries 121 properties per unit and not one of them is population,
# GDP or income; `area_sqkm` is 0 for every unit on earth. What is left is
# classification and cartographic prominence -- enough to draw the units and
# distinguish their kind, and nothing that could be mistaken for a statistic.
# Anything richer needs a second data source and a join on `iso_3166_2`.
ADMIN1_VARIABLES = {
    "type":      {"level": "nominal", "unit": None, "year_col": None},
    "type_en":   {"level": "nominal", "unit": None, "year_col": None},
    "region":    {"level": "nominal", "unit": None, "year_col": None},
    "labelrank": {"level": "ordinal", "unit": None, "year_col": None},
}

# Our World in Data, fetched through the Grapher CSV API. Country level only:
# OWID publishes by ISO3 country, which is exactly the admin_0 join key.
#
# `level` is the measurement level, and "ratio" matters: unlike "count", a zero
# is a real observation for a rate or an index, so the 0-as-no-data rule that
# protects POP_EST and GDP_MD must not apply here.
#
# To add an indicator: find its slug in the ourworldindata.org/grapher/<slug>
# URL, check the CSV's fourth column is the value you want, and add a row.
OWID_VARIABLES = {
    "life-expectancy": {
        "level": "ratio", "unit": "years", "year_col": None,
        "label": "life expectancy at birth"},
    "gdp-per-capita-worldbank": {
        "level": "ratio", "unit": "international $", "year_col": None,
        "label": "GDP per capita"},
    "co-emissions-per-capita": {
        "level": "ratio", "unit": "tonnes CO2 per person", "year_col": None,
        "label": "CO2 emissions per capita"},
    "human-development-index": {
        "level": "ratio", "unit": "index 0-1", "year_col": None,
        "label": "Human Development Index"},
    "population-density": {
        "level": "ratio", "unit": "people per km2", "year_col": None,
        "label": "population density"},
    "child-mortality": {
        "level": "ratio", "unit": "% of live births", "year_col": None,
        "label": "under-five mortality"},
    "share-of-population-in-extreme-poverty": {
        "level": "ratio", "unit": "% of population", "year_col": None,
        "label": "share in extreme poverty"},
    "share-of-individuals-using-the-internet": {
        "level": "ratio", "unit": "% of population", "year_col": None,
        "label": "internet use"},
    "median-age": {
        "level": "ratio", "unit": "years", "year_col": None,
        "label": "median age"},
    "political-regime": {
        "level": "ordinal", "unit": None, "year_col": None,
        "label": "political regime (0 closed autocracy - 3 liberal democracy)"},
}

_LEVELS = {
    "admin_0": {
        "dataset": "admin_0_countries",
        "variables": VARIABLES,
        "id": "ADM0_A3",
        "name": "NAME",
        "country": "ADM0_A3",
        "continent": "CONTINENT",
    },
    "admin_1": {
        "dataset": "admin_1_states_provinces",
        "variables": ADMIN1_VARIABLES,
        "id": "adm1_code",
        "name": "name",
        "country": "adm0_a3",
        # Admin-1 features carry no continent, so `region` there is "world" or
        # a single country code.
        "continent": None,
    },
    # Natural Earth publishes nothing below admin_1. These entries exist so the
    # level accessors answer for every canonical level; only a cartography
    # provider that serves them (overture) can actually be asked for polygons,
    # and no statistics source has variables here.
    "admin_2": {
        "dataset": None, "variables": {}, "id": "id", "name": "name",
        "country": "country", "continent": None,
    },
    "admin_3": {
        "dataset": None, "variables": {}, "id": "id", "name": "name",
        "country": "country", "continent": None,
    },
}


def scale_for(level: str) -> str:
    return SCALE if level == "admin_0" else ADMIN1_SCALE


def data_url(level: str) -> str:
    return _BASE.format(scale=scale_for(level), dataset=_LEVELS[level]["dataset"])


def dataset_for(level: str) -> str | None:
    return _LEVELS[level]["dataset"]


def variables_for(level: str, source: str = "natural_earth") -> dict:
    """Which variables a statistics source offers at a level."""
    if source == "owid":
        # OWID publishes by ISO3 country and nothing below it.
        return OWID_VARIABLES if level == "admin_0" else {}
    return _LEVELS[level]["variables"]


def sources_for(variable_id: str) -> list[str]:
    """Which statistics sources publish this variable id."""
    found = []
    if variable_id in ALL_NE_VARIABLES:
        found.append("natural_earth")
    if variable_id in OWID_VARIABLES:
        found.append("owid")
    return found


def id_property(level: str) -> str:
    return _LEVELS[level]["id"]


def name_property(level: str) -> str:
    return _LEVELS[level]["name"]


def country_property(level: str) -> str:
    return _LEVELS[level]["country"]


def continent_property(level: str) -> str | None:
    return _LEVELS[level]["continent"]


def level_of(variable_id: str) -> str | None:
    """Which admin level a variable belongs to, or None if it is not known."""
    if variable_id in OWID_VARIABLES:
        return "admin_0"
    for level, config in _LEVELS.items():
        if variable_id in config["variables"]:
            return level
    return None


# Every variable across every level and source -- the agent needs one flat enum,
# and the validator rejects a variable used at the wrong level or source.
ALL_NE_VARIABLES = {**VARIABLES, **ADMIN1_VARIABLES}
ALL_VARIABLES = {**ALL_NE_VARIABLES, **OWID_VARIABLES}
