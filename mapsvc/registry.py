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
LEVELS = ("admin_0", "admin_1", "admin_2", "admin_3",
          "nuts_1", "nuts_2", "nuts_3")
BASEMAPS = ("natural_earth", "overture", "census", "gisco")
# Overture by default: far more detail, and the only source below admin_1. It
# is queried live, so the harvest cache does the heavy lifting -- see harvest.py.
DEFAULT_BASEMAP = "overture"
DETAILS = ("simplified", "full")
METHODS = ("quantile", "equal_interval", "jenks")
PROJECTIONS = ("auto", "albers", "mercator", "mollweide")
MISSING_MODES = ("hatch", "grey", "exclude")

# Rendering is no longer part of the manifest. A map says what to show; how to
# draw it is derived, so a caller cannot pair a nominal variable with a
# sequential ramp -- the pairing is chosen from the measurement level instead of
# being validated after the fact.
DEFAULT_METHOD = "quantile"
DEFAULT_K = 5
DEFAULT_MISSING = "hatch"
RAMP_FOR_LEVEL = {
    "count": "YlGnBu", "ratio": "YlGnBu", "ordinal": "PuBuGn", "nominal": "Set2",
}

# A bbox at too fine a level returns more units than a single map can carry --
# Europe at admin_2 is 11,497 polygons and hundreds of megabytes. Counting is
# cheap (~3s) and fetching geometry is not, so the count is a pre-flight guard.
MAX_UNITS = 800
COUNT_BEFORE_FETCH = True

K_MIN, K_MAX = 3, 9

_LEVELS = {
    "admin_0": {
        "dataset": "admin_0_countries",
        "id": "ADM0_A3",
        "name": "NAME",
        "country": "ADM0_A3",
        "continent": "CONTINENT",
    },
    "admin_1": {
        "dataset": "admin_1_states_provinces",
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
        "dataset": None, "id": "id", "name": "name",
        "country": "country", "continent": None,
    },
    "admin_3": {
        "dataset": None, "id": "id", "name": "name",
        "country": "country", "continent": None,
    },
    # NUTS levels are served by GISCO, not Natural Earth.  Placeholder entries
    # keep the generic accessors total over every canonical level.
    "nuts_1": {"dataset": None, "id": "NUTS_ID", "name": "NAME_LATN",
               "country": "CNTR_CODE", "continent": None},
    "nuts_2": {"dataset": None, "id": "NUTS_ID", "name": "NAME_LATN",
               "country": "CNTR_CODE", "continent": None},
    "nuts_3": {"dataset": None, "id": "NUTS_ID", "name": "NAME_LATN",
               "country": "CNTR_CODE", "continent": None},
}


def scale_for(level: str) -> str:
    return SCALE if level == "admin_0" else ADMIN1_SCALE


def data_url(level: str) -> str:
    return _BASE.format(scale=scale_for(level), dataset=_LEVELS[level]["dataset"])


def dataset_for(level: str) -> str | None:
    return _LEVELS[level]["dataset"]


def id_property(level: str) -> str:
    return _LEVELS[level]["id"]


def name_property(level: str) -> str:
    return _LEVELS[level]["name"]


def country_property(level: str) -> str:
    return _LEVELS[level]["country"]


def continent_property(level: str) -> str | None:
    return _LEVELS[level]["continent"]


def iso3_of(props: dict) -> str | None:
    """A Natural Earth country's ISO3 join key.

    ADM0_A3 is Natural Earth's own id and differs from ISO 3166-1 for South
    Sudan (SDS, ISO SSD), Palestine (PSX/PSE), Western Sahara (SAH/ESH) and
    Aland (ALD/ALA), so joining other sources on it silently drops them.
    ISO_A3 is right where it exists; where it is -99 (Kosovo, Somaliland,
    Northern Cyprus) there is no ISO code and ADM0_A3 is the best available.
    ISO_A3_EH is not used: it gives Australia's code to Australian territories.
    """
    iso = str(props.get("ISO_A3") or "")
    if len(iso) == 3 and iso.isalpha():
        return iso.upper()
    code = props.get("ADM0_A3")
    return str(code).upper() if code else None
