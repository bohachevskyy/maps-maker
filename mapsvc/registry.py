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
SOURCES = ("natural_earth",)

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


def variables_for(level: str) -> dict:
    return _LEVELS[level]["variables"]


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
    for level, config in _LEVELS.items():
        if variable_id in config["variables"]:
            return level
    return None


# Every variable across every level -- the agent needs one flat enum, and the
# validator rejects a variable used at the wrong level.
ALL_VARIABLES = {**VARIABLES, **ADMIN1_VARIABLES}
