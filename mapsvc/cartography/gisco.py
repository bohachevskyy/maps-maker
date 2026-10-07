"""Eurostat GISCO: NUTS regions, the geography Eurostat publishes statistics on.

    https://gisco-services.ec.europa.eu/distribution/v2/nuts/geojson/
        NUTS_RG_{20M|10M}_2024_4326_LEVL_{1|2|3}.geojson

GeoJSON in EPSG:4326, so no projection or parsing work. `simplified` is the
1:20 million file, `full` the 1:10 million one. Every feature carries NUTS_ID,
which is the join key, plus its country (CNTR_CODE, ISO3_CODE) and an EU
membership flag (EU_STAT).

NUTS 2024, measured against Eurostat's nama_10r_2gdp (Feb 2026): all 244 EU
NUTS 2 regions in this file have data, while the 2021 file leaves five
unjoined (Portugal's PT16-PT18 and the Netherlands' NL31/NL33 were redrawn).
The file also holds EFTA and candidate countries -- 299 NUTS 2 regions in all.

Greece is EL and the UK UK in NUTS, not GR and GB; ISO codes are translated.

Credit is required: "© EuroGeographics for the administrative boundaries".
"""

import functools
import json

from mapsvc import fetch
from mapsvc.cartography import Boundaries, CartographyError, iso
from mapsvc.cartography.natural_earth import _intersecting

VINTAGE = 2024
URL = ("https://gisco-services.ec.europa.eu/distribution/v2/nuts/geojson/"
       "NUTS_RG_{scale}_{year}_4326_LEVL_{level}.geojson")

LEVELS = ("nuts_1", "nuts_2", "nuts_3")
KEYS = {level: ("NUTS",) for level in LEVELS}
_SCALE = {"simplified": "20M", "full": "10M"}
_SCALE_LABEL = {"20M": "1:20,000,000", "10M": "1:10,000,000"}

LICENSE = "Eurostat GISCO copyright notice"
ATTRIBUTION = "© EuroGeographics for the administrative boundaries"

# ISO 3166-1 alpha-2 -> NUTS country code, where they differ.
_NUTS_COUNTRY = {"GR": "EL", "GB": "UK"}
# Eurostat's own name for the Union, and the plain one.
_EU = {"EU", "EU27", "EU27_2020"}


@functools.lru_cache(maxsize=6)
def _features(level: str, detail: str) -> tuple:
    scale = _SCALE.get(detail, "20M")
    number = level[-1]
    url = URL.format(scale=scale, year=VINTAGE, level=number)
    try:
        data = fetch.cached(url, "gisco", f"NUTS_RG_{scale}_{VINTAGE}_LEVL_{number}.geojson")
    except fetch.FetchError as exc:
        raise CartographyError(f"could not fetch GISCO NUTS: {exc}",
                               "basemap.source") from exc
    return tuple(json.loads(data)["features"])


def _matches(props: dict, code: str) -> bool:
    if code in _EU:
        return props.get("EU_STAT") == "T"
    nuts_id = str(props.get("NUTS_ID", ""))
    if len(code) == 3 and code.isalpha() and props.get("ISO3_CODE") == code:
        return True
    if len(code) == 3 and code.isalpha():
        two = iso.to_alpha2(code) or ""
        code = two or code
    code = _NUTS_COUNTRY.get(code, code)
    # A country (FR) or any NUTS region (FR1, FRK2) contains its descendants.
    return nuts_id.startswith(code)


def _selected(level: str, bbox, within, detail: str = "simplified") -> list:
    if level not in LEVELS:
        raise CartographyError(
            f"gisco has no {level!r}; it provides {', '.join(LEVELS)}", "level")
    features = list(_features(level, detail))
    if within:
        codes = [c.strip().upper() for c in within]
        features = [f for f in features
                    if any(_matches(f["properties"], c) for c in codes)]
    if bbox is not None:
        features = _intersecting(features, bbox)
    return features


def count(level: str, bbox, within=None) -> int:
    return len(_selected(level, bbox, within))


def load(level: str, bbox, detail: str = "simplified", within=None) -> Boundaries:
    features = _selected(level, bbox, within, detail)
    if not features:
        raise CartographyError(
            f"no {level} regions " + (f"in {', '.join(within)}" if within
                                      else "in that area"),
            "within" if within else "bbox")
    units = []
    for feature in features:
        props = feature["properties"]
        code = props["NUTS_ID"]
        units.append({"id": code, "keys": {"NUTS": code},
                      "name": props.get("NAME_LATN"),
                      "geometry": feature["geometry"]})
    units.sort(key=lambda u: u["id"])
    scale = _SCALE.get(detail, "20M")
    return Boundaries(units=units, keys=KEYS[level], provenance={
        "source": "Eurostat GISCO",
        "license": LICENSE,
        "attribution": ATTRIBUTION,
        "release": f"NUTS {VINTAGE}",
        "level": level,
        "detail": detail,
        "scale": _SCALE_LABEL[scale],
        "keys": list(KEYS[level]),
    })
