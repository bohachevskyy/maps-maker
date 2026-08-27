"""Static tables: what can be mapped, and where the geometry comes from."""

# Swap to "50m" (3 MB) or "10m" (13 MB) when 110m is too coarse.
SCALE = "50m"

DATA_URL = (
    "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/"
    f"master/geojson/ne_{SCALE}_admin_0_countries.geojson"
)

SOURCE_NAME = "Natural Earth"
SOURCE_VINTAGE = "v5.1.1"

# The join key. Mostly ISO3, but carries custom codes for disputed and
# non-sovereign entities -- do not treat the two as interchangeable.
ID_PROPERTY = "ADM0_A3"
CONTINENT_PROPERTY = "CONTINENT"

# Of the 168 properties on each feature, these six are the mappable ones.
VARIABLES = {
    "POP_EST":    {"level": "count",   "unit": "people",      "year_col": "POP_YEAR"},
    "GDP_MD":     {"level": "count",   "unit": "million USD", "year_col": "GDP_YEAR"},
    "POP_RANK":   {"level": "ordinal", "unit": None,          "year_col": None},
    "INCOME_GRP": {"level": "ordinal", "unit": None,          "year_col": None},
    "ECONOMY":    {"level": "ordinal", "unit": None,          "year_col": None},
    "SUBREGION":  {"level": "nominal", "unit": None,          "year_col": None},
}

LEVELS = ("admin_0",)
METHODS = ("quantile", "equal_interval", "jenks")
PROJECTIONS = ("auto", "albers", "mercator", "mollweide")
MISSING_MODES = ("hatch", "grey", "exclude")
SOURCES = ("natural_earth",)

K_MIN, K_MAX = 3, 9
