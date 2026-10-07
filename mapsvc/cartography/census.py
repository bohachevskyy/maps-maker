"""US Census Bureau cartographic boundary files: states and counties.

    https://www2.census.gov/geo/tiger/GENZ{year}/shp/cb_{year}_us_{state|county}_{20m|5m|500k}.zip

Generalised for small-scale mapping by the Bureau itself. National views use
the 1:20,000,000 or 1:5,000,000 files; a detailed or single-state county map
uses 1:500,000. Zipped shapefiles only, read by `mapsvc.shapefile` -- no GDAL.

Keys, declared per level so statistics can only join on what is really here:

    admin_1  ISO3166_2  "US-CA"   (state FIPS -> USPS code)
    admin_2  FIPS       "06085"   (the county GEOID: state + county FIPS)

Coordinates are NAD83 longitude/latitude, which differs from WGS84 by a metre
or two in the conterminous US -- far below the resolution of either file.

The vintage matches the ACS release it is most often joined with: county
geography changes (Connecticut replaced its counties with planning regions in
2022) and statistics and boundaries of different vintages silently fail to
join. US government work, public domain.
"""

import functools

from mapsvc import fetch, shapefile, usfips
from mapsvc.cartography import Boundaries, CartographyError
from mapsvc.cartography.natural_earth import _intersecting

VINTAGE = 2024
URL = "https://www2.census.gov/geo/tiger/GENZ{year}/shp/cb_{year}_us_{kind}_{scale}.zip"

LEVELS = ("admin_1", "admin_2")
KEYS = {"admin_1": ("ISO3166_2",), "admin_2": ("FIPS",)}
_KIND = {"admin_1": "state", "admin_2": "county"}
_SCALE = {"simplified": "20m", "full": "5m"}
_SCALE_LABEL = {"20m": "1:20,000,000", "5m": "1:5,000,000",
                "500k": "1:500,000"}

LICENSE = "public domain"
ATTRIBUTION = "U.S. Census Bureau, cartographic boundary files"


def _effective_scale(level: str, detail: str, within=None, bbox=None) -> str:
    """Choose detailed counties regionally while keeping national files small."""
    named_national = any(str(code).upper() in ("US", "USA") for code in within or ())
    world_window = bbox is not None and tuple(bbox) == (-180.0, -90.0, 180.0, 90.0)
    unscoped = not within and bbox is None
    national = named_national or world_window or unscoped
    states = [code for code in within or ()
              if str(code).upper().startswith("US-")]
    single_state = len(within or ()) == 1 and len(states) == 1
    if level == "admin_2" and not national and (detail == "full" or single_state):
        return "500k"
    return _SCALE.get(detail, "20m")


@functools.lru_cache(maxsize=6)
def _features(level: str, scale: str) -> tuple:
    kind = _KIND[level]
    url = URL.format(year=VINTAGE, kind=kind, scale=scale)
    name = f"cb_{VINTAGE}_us_{kind}_{scale}.zip"
    try:
        data = fetch.cached(url, "census", name)
    except fetch.FetchError as exc:
        raise CartographyError(f"could not fetch Census boundaries: {exc}",
                               "basemap.source") from exc
    features = []
    for props, geometry in shapefile.read_zip(data):
        if geometry is not None:
            features.append({"type": "Feature", "properties": props,
                             "geometry": geometry})
    return tuple(features)


def _state_fips(code: str) -> str | None:
    code = code.strip().upper()
    if code in ("US", "USA"):
        return "*"
    return usfips.fips_of(code) if code.startswith("US-") else None


def _selected(level: str, bbox, within, detail: str = "simplified") -> list:
    if level not in LEVELS:
        raise CartographyError(
            f"census has no {level!r}; it provides {', '.join(LEVELS)} "
            "(states and counties)", "level")
    scale = _effective_scale(level, detail, within, bbox)
    features = list(_features(level, scale))
    if within:
        wanted = set()
        for code in within:
            fips = _state_fips(code)
            if fips is None:
                raise CartographyError(
                    f"census cannot scope to {code!r}: use USA, or a state as "
                    "ISO 3166-2 (US-CA)", "within")
            wanted.add(fips)
        # "USA" means the 50 states and DC. The territories (FIPS 60 and up:
        # American Samoa, Guam, the Northern Marianas, Puerto Rico, the Virgin
        # Islands) carry no data in most state-level series, so they would only
        # add hatched shapes; name one (US-PR) to get it.
        if "*" in wanted:
            wanted.discard("*")
            wanted |= {f["properties"].get("STATEFP") for f in features
                       if f["properties"].get("STATEFP", "99") < "60"}
        features = [f for f in features if f["properties"].get("STATEFP") in wanted]
    if bbox is not None:
        features = _intersecting(features, bbox)
    return features


def count(level: str, bbox, within=None) -> int:
    return len(_selected(level, bbox, within))


def load(level: str, bbox, detail: str = "simplified", within=None) -> Boundaries:
    features = _selected(level, bbox, within, detail)
    if not features:
        raise CartographyError(
            f"no census {_KIND.get(level, level)} units "
            + (f"in {', '.join(within)}" if within else "in that area"),
            "within" if within else "bbox")

    units = []
    for feature in features:
        props = feature["properties"]
        if level == "admin_1":
            code = usfips.iso_3166_2(props.get("STATEFP", ""))
            keys = {"ISO3166_2": code} if code else {}
            unit_id = code or props.get("GEOID")
        else:
            code = props.get("GEOID")
            keys = {"FIPS": code} if code else {}
            unit_id = code
        units.append({"id": unit_id, "keys": keys, "name": props.get("NAME"),
                      "geometry": feature["geometry"]})
    units.sort(key=lambda u: str(u["id"]))

    scale = _effective_scale(level, detail, within, bbox)
    return Boundaries(units=units, keys=KEYS[level], provenance={
        "source": "US Census Bureau",
        "license": LICENSE,
        "attribution": ATTRIBUTION,
        "release": f"cb_{VINTAGE}",
        "level": level,
        "detail": detail,
        "scale": _SCALE_LABEL[scale],
        "keys": list(KEYS[level]),
    })
