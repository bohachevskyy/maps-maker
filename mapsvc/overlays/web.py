"""Data found somewhere else: a CSV or GeoJSON at an https URL.

The agent supplies only the URL; the service fetches it, checks its shape and
draws it. The model never supplies a data value, so a map cannot show numbers
the model made up -- at worst it shows a real file that answers the wrong
question, and the footnote names the URL so a reader can tell.

Two ways to a URL:

* a small curated catalogue of well-known open datasets, searchable like
  OWID's charts. Entries carry what a bare file cannot: column roles, labels
  for coded categories, licence and attribution.
* any other https URL the agent proposes. Its columns are recognised by name
  (lon/long/longitude, lat/latitude, a numeric size, optional category, group
  and order); its licence is unknown and the footnote says so.

Shape decides the drawing: rows with a group or an order column and a size
are a flow (Minard's army); rows with a name and no size are places to label;
anything else with coordinates is points; GeoJSON lines are lines.
"""

import csv
import datetime
import hashlib
import http.client
import io
import ipaddress
import json
import math
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
import zipfile

from mapsvc import fetch
from mapsvc.discovery import shapefile
from mapsvc.overlays import UNKNOWN_LICENSE, Capabilities, OverlayError, Window

MAX_BYTES = 80 * 1024 * 1024    # IBTrACS North Atlantic CSV: 57.6 MB
MAX_ROWS = 50_000
MAX_URL = 500
TIMEOUT = 60
# Few enough places that every one can carry a label.
MAX_PLACES = 80
MAX_CATEGORIES = 8

CAPABILITIES = Capabilities(
    source="web",
    license="per dataset",
    attribution="per dataset",
    description="a CSV or GeoJSON file at an https URL: points (lon/lat, "
                "optional size and category), flows (rows with group/order and "
                "a size, drawn as bands like Minard's map) or labelled places. "
                "Search the curated catalogue of open datasets first; a URL "
                "found elsewhere is accepted if it fetches and has coordinates",
    id_help="an https URL of a CSV (with lon/lat columns), GeoJSON or zipped "
            "file, optionally followed by filters after '#': "
            "'<url>#season=2024', '<url>#top=30'. top=N keeps the N largest by "
            "size inside the window and the `within` countries; other filters "
            "are listed with each catalogue entry, and an entry may require one",
    searchable=True,
)

_RDATASETS = "https://raw.githubusercontent.com/vincentarelbundock/Rdatasets/master/csv/"
_NE = "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/"
_MINARD_TEMP_URL = _RDATASETS + "HistData/Minard.temp.csv"
_IBTRACS = ("https://www.ncei.noaa.gov/data/international-best-track-archive-for-"
            "climate-stewardship-ibtracs/v04r01/access/csv/")
_IBTRACS_ALL = _IBTRACS + "ibtracs.ALL.list.v04r01.csv"
IBTRACS_BASINS = {
    "NA": "North Atlantic", "EP": "Eastern North Pacific",
    "WP": "Western North Pacific", "NI": "North Indian",
    "SI": "South Indian", "SP": "South Pacific", "SA": "South Atlantic",
}
_GEONAMES = "https://download.geonames.org/export/dump/cities15000.zip"
_GVP = ("https://webservices.volcano.si.edu/geoserver/GVP-VOTW/ows?service=WFS"
        "&request=GetFeature&typeName=GVP-VOTW:Smithsonian_VOTW_Holocene_Volcanoes"
        "&outputFormat=application/json")
_MINARD = ("Charles Joseph Minard, 'Carte figurative des pertes successives en "
           "hommes de l'Armée Française dans la campagne de Russie 1812–1813' "
           "(1869), as transcribed by L. Wilkinson, The Grammar of Graphics; "
           "R HistData (M. Friendly) via Rdatasets")

CATALOGUE = (
    {
        "url": _RDATASETS + "HistData/Minard.troops.csv",
        "title": "Napoleon's march on Moscow and the retreat, 1812",
        "description": "Minard's 1812 Russian campaign: the Grande Armée's route "
                       "and size, advance and retreat, three army groups. Pair "
                       "with Minard.cities.csv for the towns along the route.",
        "keywords": "napoleon minard 1812 russia moscow army troops march retreat "
                    "campaign grande armée flow survivors war history",
        "roles": {"lon": "long", "lat": "lat", "size": "survivors",
                  "category": "direction", "group": ("group",), "order": "rownames"},
        "size_label": "soldiers", "category_label": "Direction",
        "category_labels": {"A": "Advance, June–Sept.", "R": "Retreat, Oct.–Dec."},
        "label": "Napoleon's army",
        "summary": "After Minard (1869): band width is proportional to the number "
                   "of soldiers still with the army · {start:,.0f} crossed the "
                   "Niemen, about {end:,.0f} came back",
        # The two dates are the campaign's own (the crossing of the Niemen on
        # 24 June 1812, the entry into Moscow on 14 September), as Minard's map
        # is read; the three figures are his data.
        "notes": {"start": "{v:,.0f} cross the Niemen, 24 June",
                  "turn": "{v:,.0f} reach Moscow, 14 Sept.",
                  "end": "≈ {v:,.0f} recross the Niemen"},
        # Natural Earth's names for the rivers Minard's army crossed, in the
        # English of his time.
        "river_names": {"Nemunas": "Niemen", "Dnepre": "Dnieper", "Berezina": "Berezina"},
        "license": "public domain (1869 figures; transcription distributed in R HistData)",
        "attribution": _MINARD,
        "companions": (_MINARD_TEMP_URL,),
    },
    {
        "url": _RDATASETS + "HistData/Minard.cities.csv",
        "title": "Towns on Minard's map of the 1812 campaign",
        "description": "The 20 towns Minard named along Napoleon's route (Kowno, "
                       "Wilna, Smolensk, Moscou...), with coordinates. Use with "
                       "Minard.troops.csv.",
        "keywords": "napoleon minard 1812 russia moscow cities towns places labels",
        "roles": {"lon": "long", "lat": "lat", "name": "city"},
        "major": ("Kowno", "Wilna", "Smolensk", "Moscou"),
        "label": "Towns on Minard's map",
        "license": "public domain (1869); transcription distributed in R HistData",
        "attribution": _MINARD,
    },
    {
        "url": _MINARD_TEMP_URL,
        "title": "Temperatures during Napoleon's retreat from Moscow, 1812",
        "description": "Minard's temperature observations along the retreat, with dates. "
                       "Temperatures are recorded in degrees Réaumur and converted to °C.",
        "keywords": "napoleon minard 1812 russia moscow retreat temperature weather dates",
        "kind": "temperature",
        "roles": {"lon": "long", "temperature": "temp", "days": "days",
                  "date": "date"},
        "label": "Temperature on the retreat",
        "license": "public domain (1869); transcription distributed in R HistData",
        "attribution": _MINARD,
    },
    {
        "url": _IBTRACS_ALL,
        "title": "Global tropical cyclones by basin, 1842–present "
                 "(NOAA IBTrACS)",
        "description": "Best tracks of every tropical cyclone in any IBTrACS basin, "
                       "three-hourly positions with wind and Saffir-Simpson "
                       "category, one line per storm. Updated weekly, so the "
                       "latest season is included (provisional until NHC's "
                       "post-season review).",
        "keywords": "hurricane hurricanes typhoon typhoons storm storms tropical cyclone cyclones "
                    "atlantic pacific indian caribbean gulf mexico track tracks wind noaa nhc "
                    "hurdat ibtracs best track saffir simpson category season "
                    "named landfall",
        "format": "ibtracs",
        "filters": {"season": {"min": 1842, "max": "this year",
                               "help": "the storm season, a year"},
                    "basin": {"choices": tuple(IBTRACS_BASINS),
                              "help": "IBTrACS basin: NA, EP, WP, NI, SI, SP or SA"}},
        "required_filters": ("basin", "season"),
        # Measured in NOAA's 2026-10-25 directory: ALL is 333,098,313 bytes;
        # the largest basin file is WP at 114,661,893 bytes. Fetching the
        # selected basin keeps the cap comfortably below the all-basins file.
        "max_bytes": 128 * 1024 * 1024,
        "label": "Storm tracks",
        "license": "public domain (NOAA)",
        "attribution": "NOAA NCEI, International Best Track Archive for Climate "
                       "Stewardship (IBTrACS) v04r01; Knapp et al. (2010), "
                       "doi:10.25921/82ty-9e16",
    },
    {
        "url": _RDATASETS + "datasets/quakes.csv",
        "title": "Earthquakes off Fiji, 1,000 events since 1964",
        "description": "1,000 seismic events of M4+ near Fiji (Harvard), with depth "
                       "and magnitude. For current earthquakes use the usgs source.",
        "keywords": "earthquakes fiji tonga seismic quakes magnitude depth",
        "roles": {"lon": "long", "lat": "lat", "size": "mag"},
        "size_label": "Magnitude", "scale": "magnitude",
        "label": "Earthquakes off Fiji",
        "license": "public domain (R datasets)",
        "attribution": "Harvard PRIM-H project, R datasets 'quakes', via Rdatasets",
    },
    {
        "url": _NE + "ne_10m_populated_places_simple.geojson",
        "title": "Populated places of the world (Natural Earth)",
        "description": "7,342 cities and towns with population estimates; points "
                       "sized by population.",
        "keywords": "cities towns population places capitals urban natural earth",
        "roles": {"size": "pop_max", "name": "name", "country": "adm0_a3"},
        "size_label": "Population, largest estimate (often the urban area)",
        "label": "Cities", "rank_noun": "cities",
        "license": "public domain", "attribution": "Natural Earth",
    },
    {
        "url": _GEONAMES,
        "title": "Cities of the world with population (GeoNames cities15000)",
        "description": "About 34,000 cities of 15,000+ people, with the population "
                       "of the city proper (the administrative city, not the "
                       "metropolitan area) and country. For the largest N "
                       "anywhere: top=N, scoped by the map window and `within`.",
        "keywords": "cities city largest biggest population populous towns urban "
                    "places capitals geonames ranking top most",
        "format": "geonames",
        "max_bytes": 20 * 1024 * 1024,
        "required_filters": ("top",),
        "roles": {"lon": "longitude", "lat": "latitude", "size": "population",
                  "name": "name", "country": "country_code"},
        # Sections of a city (PPLX: Pest, Brooklyn) would count it twice;
        # historical, abandoned and destroyed places are not cities now.
        "exclude": {"feature_code": ("PPLX", "PPLH", "PPLQ", "PPLW", "PPLCH")},
        "exclude_reason": "districts of a city, or historical, abandoned or "
                          "destroyed places",
        # Said under the map when the city is on it: GeoNames carries the
        # latest figure its sources give, which for a few cities is not the
        # city proper's current one.
        # Measured 2026-10-08 in cities15000: Moscow 10,381,222 -- near the 2002
        # census, well below the 2021 census (13.0 M); Stockholm 1,515,017 and
        # Copenhagen 1,153,615, well above their municipalities.
        "caveats": {"Moscow": "an older figure, below the 2021 census count of 13.0 million",
                    "Stockholm": "a wider urban-area figure",
                    "Copenhagen": "a wider urban-area figure"},
        "size_label": "Population, city proper",
        "label": "Cities", "rank_noun": "cities",
        "definition": "population of the city proper as recorded by GeoNames "
                      "(administrative city limits, not the metropolitan area)",
        "license": "CC BY 4.0", "attribution": "GeoNames (geonames.org), cities15000",
    },
    {
        "url": _GVP,
        "title": "Holocene volcanoes of the world (Smithsonian GVP)",
        "description": "About 1,200 volcanoes with eruptions or unrest in the last "
                       "~11,700 years (the Holocene), from the Smithsonian Global "
                       "Volcanism Program, with name, country and volcano type.",
        "keywords": "volcano volcanoes volcanic eruption eruptions active holocene "
                    "10000 years ring fire smithsonian gvp stratovolcano caldera "
                    "shield geology",
        "roles": {"name": "Volcano_Name", "category": "Primary_Volcano_Type"},
        "category_label": "Volcano type",
        # GVP's 28 primary types, collapsed to the families a world map can
        # tell apart. Complex and compound volcanoes are composite edifices.
        "category_map": {
            "Stratovolcano": "strato", "Stratovolcano(es)": "strato",
            "Stratovolcano?": "strato", "Complex": "strato", "Compound": "strato",
            "Shield": "shield", "Shield(s)": "shield", "Shield(pyroclastic)": "shield",
            "Caldera": "caldera", "Caldera(s)": "caldera",
            "Volcanic field": "field",
            "Fissure vent": "fissure", "Fissure vent(s)": "fissure",
            "Crater rows": "fissure",
            "Lava dome": "small", "Lava dome(s)": "small", "Pyroclastic cone": "small",
            "Pyroclastic cone(s)": "small", "Cone": "small", "Lava cone": "small",
            "Lava cone(s)": "small", "Lava cone(es)": "small", "Tuff cone": "small",
            "Tuff cone(s)": "small", "Tuff ring(s)": "small", "Maar": "small",
            "Maar(s)": "small", "Explosion crater(s)": "small",
        },
        "category_order": ("strato", "shield", "caldera", "field", "fissure",
                           "small", "other"),
        "category_labels": {"strato": "Stratovolcano or complex",
                            "shield": "Shield", "caldera": "Caldera",
                            "field": "Volcanic field", "fissure": "Fissure vents",
                            "small": "Cones, domes and maars",
                            "other": "Type not recorded"},
        "category_other": "other",
        "label": "Holocene volcanoes", "label_top": 15, "noun": "volcanoes",
        "map_title": "Holocene volcanoes of the world",
        "marker": "triangle",
        # Hues far enough apart to tell at 3 px: red, orange, purple, blue,
        # green and brown; the unrecorded grey.
        "category_colours": {"strato": "#c8372d", "shield": "#ee8a1e",
                             "caldera": "#7a4aa3", "field": "#2f7fc1",
                             "fissure": "#2e9e5b", "small": "#8a5a2b",
                             "other": "#8c8c8c"},
        "summary_note": "active within the last ~11,700 years (the Holocene, "
                        "the Global Volcanism Program's definition, a little "
                        "longer than 10,000)",
        "note": "Holocene volcanoes are those with eruptions or unrest since the "
                "last ice age (~11,700 years); the best-documented are named, "
                "ranked by what each record reports of its consequences",
        "note_fame": "Holocene volcanoes are those with eruptions or unrest since "
                     "the last ice age (~11,700 years); the 15 named are the "
                     "best known, ranked by how many Wikipedia language editions "
                     "describe the volcano (Wikidata, CC0), weighted by how "
                     "recently it erupted",
        "fame": {"id": "Volcano_Number", "property": "P1886"},
        "notability": {"text": "Geological_Summary", "year": "Last_Eruption_Year",
                       "size": "Elevation"},
        "theme": "seismic", "category_counts": True,
        "license": "Smithsonian terms of use; cite as requested",
        "attribution": "Global Volcanism Program, 2026. Volcanoes of the World "
                       "(v. 5.4.0; 7 Aug 2026). Distributed by Smithsonian "
                       "Institution, compiled by Venzke, E. "
                       "doi:10.5479/si.GVP.VOTW5-2026.5.4",
    },
    {
        "url": _NE + "ne_10m_airports.geojson",
        "title": "Airports of the world (Natural Earth)",
        "description": "891 airports, with name, IATA code and type.",
        "keywords": "airports aviation flights airfields iata natural earth",
        "roles": {"name": "name"}, "label": "Airports",
        "license": "public domain", "attribution": "Natural Earth",
    },
    {
        "url": _NE + "ne_10m_ports.geojson",
        "title": "Sea ports of the world (Natural Earth)",
        "description": "1,081 ports, with name.",
        "keywords": "ports harbours harbors shipping maritime sea natural earth",
        "roles": {"name": "name"}, "label": "Ports",
        "license": "public domain", "attribution": "Natural Earth",
    },
)
_BY_URL = {entry["url"]: entry for entry in CATALOGUE}
# Backward-compatible direct North Atlantic ids remain valid; discovery offers
# the general all-basins entry and requires an explicit basin.
_IBTRACS_NA = _IBTRACS + "ibtracs.NA.list.v04r01.csv"
_BY_URL[_IBTRACS_NA] = {
    **next(entry for entry in CATALOGUE if entry["url"] == _IBTRACS_ALL),
    "url": _IBTRACS_NA,
    "title": "North Atlantic hurricanes and tropical storms, 1851–present (NOAA IBTrACS)",
    "filters": {"season": {"min": 1851, "max": "this year",
                            "help": "the storm season, a year"}},
    "required_filters": ("season",),
    "max_bytes": 100 * 1024 * 1024,
}


def _discovered_entry(url: str) -> dict:
    """A catalogue-like entry for a point/line dataset the research step validated.

    Its column roles, title, publisher and licence come from the discovered-
    sources store, so an arbitrary validated URL is drawn and credited like a
    curated catalogue entry. {} when the URL was never validated.
    """
    try:
        from mapsvc.discovery.store import Store
        rows = [r for r in Store().by_url(url)
                if r.get("geometry_kind") in ("points", "lines")]
    except Exception:
        return {}
    if not rows:
        return {}
    rec = rows[0]
    proposed = rec.get("roles") or {}
    roles = {k: v for k, v in proposed.items()
             if k in ("lon", "lat", "end_lon", "end_lat", "name", "category", "date", "size")}
    if "value" in proposed and "size" not in roles:
        roles["size"] = proposed["value"]
    when = (rec.get("validated_at") or "")[:10]
    return {
        "url": url, "title": rec.get("title"), "label": rec.get("title"),
        "description": f"{rec.get('publisher') or 'web'}, {rec.get('vintage') or 'undated'} "
                       "(found by the research step)",
        "roles": roles, "license": rec.get("licence"),
        "discovered": True, "format_found": rec.get("format"),
        "row_filter": proposed.get("filter") or [],
        "attribution": (f"{rec.get('publisher') or 'unknown publisher'} ({url}; found by the "
                        "research step" + (f", validated {when}" if when else "") + ")"),
    }


def companions(url: str) -> tuple[str, ...]:
    """Catalogue companions derived from a requested dataset."""
    return tuple(_BY_URL.get(url, {}).get("companions") or ())


# ------------------------------------------------------------------ parse ---

# What to show, after the URL: "#season=2024", "#top=30". Never how to draw.
# `top` applies to any points dataset with a size column; the rest are
# declared by a catalogue entry.
MAX_TOP = 200
_FILTER = re.compile(r"([a-z_]{1,20})=([A-Za-z0-9_]{1,20})")
GENERIC_FILTERS = {"top": {"min": 1, "max": MAX_TOP,
                           "help": "keep the N largest by size within the map "
                                   "window and `within`"}}


def split(overlay_id: str) -> tuple[str, dict]:
    """A canonical id's URL and its validated filters."""
    url, _, fragment = overlay_id.partition("#")
    filters = {}
    for part in filter(None, fragment.split(";")):
        name, _, value = part.partition("=")
        filters[name] = int(value) if value.isdigit() else value
    return url, filters


def filters_for(url: str) -> dict:
    """Every filter a URL accepts: the generic ones plus its entry's own."""
    return {**GENERIC_FILTERS, **(_BY_URL.get(url, {}).get("filters") or {})}


def _bound(value, today_year: int) -> int:
    return today_year if value == "this year" else int(value)


def parse(overlay_id: str) -> str:
    """An https URL, refused if it could reach this machine or its network,
    plus filters after '#' checked against what the dataset accepts."""
    url, _, fragment = overlay_id.strip().partition("#")
    url = _parse_url(url)
    entry = _BY_URL.get(url) or _discovered_entry(url)
    accepted = filters_for(url)
    this_year = datetime.date.today().year
    filters: dict = {}
    for part in fragment.split(";") if fragment else ():
        match = _FILTER.fullmatch(part.strip())
        if not match:
            raise OverlayError(
                f"web overlay filter {part!r} is not name=integer; this dataset "
                f"accepts {_filter_help(url)}", "overlays.id")
        name, raw_value = match.group(1), match.group(2)
        spec = accepted.get(name)
        if spec is None:
            raise OverlayError(
                f"web overlay filter {name!r} is not one this dataset accepts; "
                f"it accepts {_filter_help(url)}", "overlays.id")
        if "choices" in spec:
            value = raw_value.upper()
            if value not in spec["choices"]:
                raise OverlayError(f"web overlay filter {name}={raw_value} must be one of "
                                   f"{', '.join(spec['choices'])}", "overlays.id")
        else:
            if not raw_value.isdigit():
                raise OverlayError(f"web overlay filter {name}={raw_value} must be an integer",
                                   "overlays.id")
            value = int(raw_value)
            low, high = _bound(spec["min"], this_year), _bound(spec["max"], this_year)
            if not low <= value <= high:
                raise OverlayError(f"web overlay filter {name}={value} is outside "
                                   f"{low}-{high}", "overlays.id")
        if name in filters:
            raise OverlayError(f"web overlay filter {name!r} is given twice",
                               "overlays.id")
        filters[name] = value
    missing = [f for f in entry.get("required_filters") or () if f not in filters]
    if missing:
        raise OverlayError(
            f"{entry.get('title') or url} needs {', '.join(missing)}: add "
            f"'#{missing[0]}=...' to the URL ({_filter_help(url)})", "overlays.id")
    if not filters:
        return url
    return url + "#" + ";".join(f"{k}={filters[k]}" for k in sorted(filters))


def _filter_help(url: str) -> str:
    this_year = datetime.date.today().year
    parts = []
    for name, spec in sorted(filters_for(url).items()):
        accepted = ("|".join(spec["choices"]) if "choices" in spec else
                    f"{_bound(spec['min'], this_year)}-{_bound(spec['max'], this_year)}")
        parts.append(f"{name}=<{accepted}> ({spec['help']})")
    return "; ".join(parts)


def _parse_url(url: str) -> str:
    url = url.strip()
    if len(url) > MAX_URL or any(c.isspace() for c in url):
        raise OverlayError("web overlay id must be one https URL", "overlays.id")
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https":
        raise OverlayError(f"web overlay URLs must be https, got {url!r}", "overlays.id")
    if parts.username or parts.password:
        raise OverlayError("web overlay URLs must not carry credentials", "overlays.id")
    host = (parts.hostname or "").lower()
    if not host or "." not in host or host.endswith((".local", ".internal", ".localhost")):
        raise OverlayError(f"web overlay URL has no public host: {url!r}", "overlays.id")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise OverlayError("web overlay URLs must point at a public host", "overlays.id")
    return url


def _check_resolves_public(host: str) -> None:
    """At fetch time: every address the host resolves to must be public."""
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except OSError as exc:
        raise OverlayError(f"could not resolve {host}: {exc}", "overlays.id") from exc
    for info in infos:
        if not ipaddress.ip_address(info[4][0]).is_global:
            raise OverlayError(f"{host} resolves to a non-public address",
                               "overlays.id")


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _parse_url(newurl.partition("#")[0])
        _check_resolves_public(urllib.parse.urlsplit(newurl).hostname)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _download(url: str, limit: int = MAX_BYTES) -> bytes:
    _parse_url(url)
    _check_resolves_public(urllib.parse.urlsplit(url).hostname)
    opener = urllib.request.build_opener(_SafeRedirect)
    request = urllib.request.Request(url, headers={"User-Agent": fetch.USER_AGENT})
    try:
        with opener.open(request, timeout=TIMEOUT) as response:
            payload = response.read(limit + 1)
    except urllib.error.HTTPError as exc:
        raise OverlayError(f"{url} returned HTTP {exc.code}", "overlays.id") from exc
    except (OSError, http.client.HTTPException) as exc:
        raise OverlayError(f"could not fetch {url}: {exc}", "overlays.id") from exc
    if len(payload) > limit:
        raise OverlayError(f"{url} is larger than {limit // 2**20} MB",
                           "overlays.id")
    return payload


def fetch_bytes(url: str) -> bytes:
    """The file at a URL, cached by the URL alone: filters select from it."""
    base_url, filters = split(url)
    download_url = base_url
    if base_url == _IBTRACS_ALL and filters.get("basin") in IBTRACS_BASINS:
        # The all-basins CSV is hundreds of MB. NOAA publishes the same schema
        # split by basin, so stream/cache only the validated basin requested.
        basin = filters["basin"]
        download_url = _IBTRACS + f"ibtracs.{basin}.list.v04r01.csv"
    digest = hashlib.sha256(download_url.encode()).hexdigest()[:16]
    target = fetch.path("overlays", "web", digest)
    if target.exists():
        return target.read_bytes()
    # A curated entry may be larger than an arbitrary URL is allowed to be.
    payload = _download(download_url, _BY_URL.get(base_url, {}).get("max_bytes") or MAX_BYTES)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".part")
    temporary.write_bytes(payload)
    temporary.replace(target)
    return payload


# ----------------------------------------------------------------- search ---

class Candidate:
    def __init__(self, entry: dict):
        self.id = entry["url"]
        self.title = entry["title"]
        self.subtitle = entry["description"]
        self.license = entry["license"]
        self.filters = _filter_help(entry["url"])
        self.required = tuple(entry.get("required_filters") or ())

    def as_prompt_line(self) -> str:
        needs = (f" Requires {', '.join(self.required)}." if self.required else "")
        return (f"{self.id}: {self.title} -- {self.subtitle} [{self.license}]. "
                f"Filters after '#': {self.filters}.{needs}")


def search(query: str, limit: int = 8) -> list:
    words = set(re.findall(r"[a-z0-9]+", query.lower()))
    scored = []
    explicit_na = words & {"atlantic", "caribbean", "gulf"}
    generic_hurricane = ("hurricane" in words or "hurricanes" in words) and not (
        words & {"global", "world", "pacific", "indian", "typhoon", "typhoons", "cyclone", "cyclones"})
    if explicit_na or generic_hurricane:
        # A specifically named basin can use NOAA's smaller per-basin file;
        # unspecified/global requests get the general entry and basin filter.
        scored.append((-100, -1, _BY_URL[_IBTRACS_NA]))
    for i, entry in enumerate(CATALOGUE):
        haystack = set(re.findall(r"[a-z0-9]+", " ".join(
            (entry["title"], entry["description"], entry["keywords"])).lower()))
        score = len(words & haystack)
        if score:
            scored.append((-score, i, entry))
    scored.sort(key=lambda t: (t[0], t[1]))
    found = [Candidate(entry) for _, _, entry in scored[:limit]]
    return found + _discovered_candidates(query, limit - len(found))


def _discovered_candidates(query: str, limit: int) -> list:
    """Point and line datasets the research step validated earlier."""
    if limit <= 0:
        return []
    try:
        from mapsvc.discovery.store import Store
        store = Store()
        recs = [r for kind in ("points", "lines")
                for r in store.search_any(query, geometry_kind=kind, limit=limit)]
    except Exception:
        return []
    entries = [_discovered_entry(r["url"]) for r in recs[:limit]]
    return [Candidate(e) for e in entries if e]


# ------------------------------------------------------------------- load ---

_LON = ("lon", "long", "longitude", "lng", "x")
_LAT = ("lat", "latitude", "y")
_SIZE = ("survivors", "value", "count", "size", "population", "pop", "pop_max",
         "magnitude", "mag", "amount", "total", "n", "wind")
_CATEGORY = ("direction", "category", "type", "class", "kind", "status")
_GROUP = ("group", "series", "track", "route", "trip")
_ORDER = ("order", "seq", "sequence", "step", "index")
_NAME = ("name", "city", "label", "place", "title", "town")


def check(overlay_id: str) -> None:
    """Fetch and shape-check an id without a window; raises OverlayError."""
    world = Window(None, None, (-180.0, -90.0, 180.0, 90.0))
    layer_from_bytes(fetch_bytes(overlay_id), overlay_id, world)


def load(overlay_id: str, window: Window) -> dict:
    payload = fetch_bytes(overlay_id)
    entry = _BY_URL.get(split(overlay_id)[0]) or _discovered_entry(split(overlay_id)[0])
    fame = _fame(entry["fame"]) if entry.get("fame") else None
    return layer_from_bytes(payload, overlay_id, window, fame)


WIKIDATA = "https://query.wikidata.org/sparql"


def _fame(spec: dict) -> dict | None:
    """Wikipedia language editions per item, by an external id: how well known
    each row is, from Wikidata (CC0). {id: sitelinks}; None when Wikidata
    cannot be reached -- the map is then named by its own records instead.

    One query for every item that carries the identifier property, cached
    like any download: sitelink counts move by a few a year.
    """
    query = (f"SELECT ?id ?links WHERE {{ ?item wdt:{spec['property']} ?id . "
             "?item wikibase:sitelinks ?links }")
    url = WIKIDATA + "?" + urllib.parse.urlencode({"query": query, "format": "json"})
    name = hashlib.sha256(url.encode()).hexdigest()[:16]
    try:
        payload = fetch.cached(url, "overlays", "web", f"fame-{name}.json",
                               accept="application/sparql-results+json")
        bindings = json.loads(payload)["results"]["bindings"]
    except (OSError, ValueError, KeyError):
        return None
    out: dict = {}
    for binding in bindings:
        try:
            ident, links = binding["id"]["value"], int(binding["links"]["value"])
        except (KeyError, ValueError):
            continue
        out[ident] = max(out.get(ident, 0), links)
    return out or None


def _renown(links: int, year) -> float:
    """Fame from Wikipedia editions (log scale, 0-1 at ~150) weighted by the
    eruption record: erupted since 1900 counts in full, in the 1700s a little
    less, in the historical era half, long ago or unknown a fraction -- an
    island that was a volcano once is not the volcano a map needs named."""
    if year is None:
        recency = 0.1
    elif year >= 1900:
        recency = 1.0
    elif year >= 1700:
        recency = 0.85
    elif year >= 0:
        recency = 0.5
    else:
        recency = 0.15
    return round(min(1.0, math.log1p(links) / math.log1p(150)) * (0.4 + 0.6 * recency), 4)


def layer_from_bytes(payload: bytes, overlay_id: str, window: Window,
                     fame: dict | None = None) -> dict:
    url, filters = split(overlay_id)
    entry = _BY_URL.get(url) or _discovered_entry(url)
    if fame is not None and entry.get("note_fame"):
        entry = {**entry, "note": entry["note_fame"]}
    payload = _unzip(payload, url)
    text = payload.decode("utf-8-sig", errors="replace")
    host = urllib.parse.urlsplit(url).hostname
    provenance = {
        "name": entry.get("attribution") or host,
        "attribution": entry.get("attribution") or host,
        "license": entry.get("license") or UNKNOWN_LICENSE,
        "url": url,
        "vintage": None,
    }
    base = {"source": "web", "id": overlay_id, "provenance": provenance,
            **({"theme": entry["theme"]} if entry.get("theme") else {}),
            "title": entry.get("title") or f"Data from {host}",
            **({"map_title": entry["map_title"]} if entry.get("map_title") else {}),
            "label": entry.get("label") or entry.get("title") or host}
    if entry.get("format") == "ibtracs":
        return _ibtracs_layer(base, text, filters, window)
    if entry.get("format") == "geonames":
        records, columns = _geonames(text, url)
        lines = []
    elif (entry.get("discovered")
          and entry.get("format_found") not in {"geojson", "shapefile"}):
        # What was validated is what is drawn: the same parser (CSV, XLSX,
        # JSON APIs, zips) and the same row conditions as at validation.
        from mapsvc.discovery import probe, rowfilter
        records, _fmt, _geo, _warn = probe.records(payload)
        records = rowfilter.apply(records, entry.get("row_filter"))
        columns = list(dict.fromkeys(k for r in records[:200] for k in r))
        lines = _segments(records, entry.get("roles") or {})
    elif text.lstrip().startswith("{"):
        records, columns, lines = _geojson(text, url)
        if entry.get("discovered") and not lines:
            from mapsvc.discovery import rowfilter
            records = rowfilter.apply(records, entry.get("row_filter"))
    else:
        records, columns = _csv(text, url)
        lines = []
    roles = dict(entry.get("roles") or _guess_roles(columns, records))
    if "__lon" in columns:
        roles.setdefault("lon", "__lon")
        roles.setdefault("lat", "__lat")
    records, dropped = _exclude(records, entry)
    if entry.get("kind") == "temperature":
        return _temperature_layer(base, records, roles)
    if lines:
        return _lines_layer(base, lines, roles, window)
    if "lon" not in roles or "lat" not in roles:
        raise OverlayError(
            f"{url} has no recognisable longitude/latitude columns (found "
            f"{', '.join(columns[:12])})", "overlays.id")
    rows, bad = _rows(records, roles, entry, fame)
    if fame is not None:
        provenance["attribution"] += "; renown: Wikidata sitelink counts (CC0)"
        provenance["license"] += "; Wikidata CC0"
    if not rows:
        raise OverlayError(f"{url} has no rows with numeric coordinates", "overlays.id")
    if len(rows) > MAX_ROWS:
        raise OverlayError(f"{url} has {len(rows):,} rows, over {MAX_ROWS:,}",
                           "overlays.id")
    excluded = list(dropped)
    if bad:
        excluded.append({"count": bad, "reason": "rows without numeric coordinates"})
    has_size = "size" in roles
    if "top" in filters:
        if not has_size:
            raise OverlayError(f"{url} has no numeric size column, so it has no "
                               "largest N; drop the top filter", "overlays.id")
        return _ranked_layer(base, rows, roles, entry, excluded, window, filters["top"])
    if has_size and ("group" in roles or "order" in roles):
        return _flow_layer(base, rows, roles, entry, excluded)
    if (not has_size and "name" in roles and "category" not in roles
            and len(rows) <= MAX_PLACES):
        return _places_layer(base, rows, entry, excluded)
    return _points_layer(base, rows, roles, entry, excluded, window)


# A zip must hold one data file, read in memory and never extracted to disk;
# its declared size and its compression ratio are both bounded before reading.
MAX_UNZIPPED = 200 * 1024 * 1024   # NFDB points: 79.4 MB of text (measured 2026-11-07)
MAX_RATIO = 40


def _unzip(payload: bytes, url: str) -> bytes:
    if not payload.startswith(b"PK\x03\x04"):
        return payload
    try:
        archive = zipfile.ZipFile(io.BytesIO(payload))
        if any(shapefile._usable(m) and m.filename.lower().endswith(".shp")
               for m in archive.infolist()):
            try:
                converted = shapefile.to_geojson(payload)
            except shapefile.ShapefileError as exc:
                raise OverlayError(f"{url} has an unreadable shapefile: {exc}",
                                   "overlays.id") from exc
            return json.dumps(converted, sort_keys=True,
                              separators=(",", ":")).encode("utf-8")
        members = [m for m in archive.infolist()
                   if not m.is_dir() and not m.filename.startswith(("__MACOSX/", "."))
                   and m.filename.lower().endswith((".csv", ".txt", ".tsv", ".json",
                                                    ".geojson"))]
        if not members:
            raise OverlayError(f"{url} is a zip with no data files", "overlays.id")
        # Several: the largest is the data (the same rule as discovery.probe).
        member = sorted(members, key=lambda m: (-m.file_size, m.filename))[0]
        if member.flag_bits & 0x1:
            raise OverlayError(f"{url} is an encrypted zip", "overlays.id")
        if (member.file_size > MAX_UNZIPPED
                or member.file_size > MAX_RATIO * max(1, member.compress_size)):
            raise OverlayError(f"{url} unpacks to {member.file_size:,} bytes, over "
                               f"the {MAX_UNZIPPED // 2**20} MB limit or ratio",
                               "overlays.id")
        with archive.open(member) as handle:
            data = handle.read(MAX_UNZIPPED + 1)
    except (zipfile.BadZipFile, zipfile.LargeZipFile, NotImplementedError,
            RuntimeError, OSError) as exc:
        raise OverlayError(f"{url} is not a readable zip: {exc}", "overlays.id") from exc
    if len(data) > MAX_UNZIPPED:
        raise OverlayError(f"{url} unpacks to more than {MAX_UNZIPPED // 2**20} MB",
                           "overlays.id")
    return data


# The `geoname` table, as documented in download.geonames.org/export/dump.
GEONAMES_COLUMNS = (
    "geonameid", "name", "asciiname", "alternatenames", "latitude", "longitude",
    "feature_class", "feature_code", "country_code", "cc2", "admin1_code",
    "admin2_code", "admin3_code", "admin4_code", "population", "elevation", "dem",
    "timezone", "modification_date")


def _geonames(text: str, url: str):
    records = []
    for line in text.splitlines():
        cells = line.split("\t")
        if len(cells) != len(GEONAMES_COLUMNS):
            continue
        records.append(dict(zip(GEONAMES_COLUMNS, cells)))
    if not records:
        raise OverlayError(f"{url} has no rows in the GeoNames layout", "overlays.id")
    return records, list(GEONAMES_COLUMNS)


def _exclude(records: list, entry: dict) -> tuple[list, list]:
    """Rows a catalogue entry says are not features of their own."""
    rules = entry.get("exclude") or {}
    if not rules:
        return records, []
    kept, dropped = [], {}
    for record in records:
        for column, values in sorted(rules.items()):
            if str(record.get(column) or "") in values:
                dropped[column] = dropped.get(column, 0) + 1
                break
        else:
            kept.append(record)
    why = entry.get("exclude_reason") or (
        "parts of another place, or not a place today")
    return kept, [{"count": n, "reason": why} for column, n in sorted(dropped.items())]


def _csv(text: str, url: str):
    try:
        reader = csv.DictReader(io.StringIO(text))
        records = list(reader)
    except csv.Error as exc:
        raise OverlayError(f"{url} is not a readable CSV: {exc}", "overlays.id") from exc
    columns = list(reader.fieldnames or [])
    if not columns or not records:
        raise OverlayError(f"{url} is empty or not a CSV", "overlays.id")
    return records, columns


def _geojson(text: str, url: str):
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise OverlayError(f"{url} is not valid JSON: {exc}", "overlays.id") from exc
    features = data.get("features") if isinstance(data, dict) else None
    if not isinstance(features, list):
        raise OverlayError(f"{url} is not a GeoJSON FeatureCollection", "overlays.id")
    records, lines = [], []
    for i, f in enumerate(features):
        geometry = (f or {}).get("geometry") or {}
        props = dict((f or {}).get("properties") or {})
        kind = geometry.get("type")
        coords = geometry.get("coordinates")
        if kind == "Point" and coords:
            props["__lon"], props["__lat"] = coords[0], coords[1]
            records.append(props)
        elif kind in ("LineString", "MultiLineString") and coords:
            parts = [coords] if kind == "LineString" else coords
            for j, part in enumerate(parts):
                lines.append((f"{i}.{j}", props, part))
    if not records and not lines:
        raise OverlayError(f"{url} has no Point or LineString features; polygons are "
                           "boundaries, not overlays", "overlays.id")
    columns = sorted({k for r in records for k in r} | {k for _, p, _ in lines for k in p})
    return records, columns, lines


def _guess_roles(columns: list, records: list) -> dict:
    lowered = {c.lower(): c for c in columns}
    roles = {}
    if "__lon" in columns:
        roles["lon"], roles["lat"] = "__lon", "__lat"
    for role, names in (("lon", _LON), ("lat", _LAT), ("name", _NAME),
                        ("order", _ORDER)):
        if role in roles:
            continue
        for name in names:
            if name in lowered:
                roles[role] = lowered[name]
                break
    for name in _GROUP:
        if name in lowered:
            roles["group"] = (lowered[name],)
            break
    for name in _SIZE:
        column = lowered.get(name)
        if column and _numeric_share(records, column) > 0.9:
            roles["size"] = column
            break
    for name in _CATEGORY:
        column = lowered.get(name)
        if column and 1 < len({str(r.get(column)) for r in records}) <= MAX_CATEGORIES:
            roles["category"] = column
            break
    return roles


def _number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _temperature_layer(base: dict, records: list, roles: dict) -> dict:
    """Minard's dated Réaumur observations as a below-map panel layer."""
    needed = ("lon", "temperature", "date")
    if any(role not in roles for role in needed):
        raise OverlayError("temperature data needs longitude, temperature and date columns",
                           "overlays.id")
    features, bad = [], 0
    for i, record in enumerate(records):
        lon = _number(record.get(roles["lon"]))
        reaumur = _number(record.get(roles["temperature"]))
        if lon is None or reaumur is None:
            bad += 1
            continue
        days = _number(record.get(roles.get("days"))) if roles.get("days") else None
        features.append({"id": i, "lon": round(lon, 5),
                         "temperature_r": reaumur,
                         "temperature_c": reaumur * 1.25,
                         "days": days,
                         "date": str(record.get(roles["date"]) or "").strip()})
    if not features:
        raise OverlayError("temperature data has no numeric observations", "overlays.id")
    excluded = ([{"count": bad, "reason": "rows without numeric longitude or temperature"}]
                if bad else [])
    return {**base, "kind": "temperature", "features": features,
            "summary": "Réaumur observations converted to °C",
            "encoding": "Temperature line in °C (original Réaumur values multiplied by 1.25)",
            "excluded": excluded}


# What a catalogue's own prose says makes a place worth naming on a map that
# cannot name every one: deaths, destruction, tsunamis, eruptions of record.
_NOTABLE = re.compile(
    r"killed|deadl|fatal|deaths|victims|devastat|catastroph|largest|famous|"
    r"best[- ]known|most active|buried|destroyed|tsunami|evacuat|Pompeii|VEI|"
    r"climate|Plinian|pyroclastic flows", re.I)


def _notability(record, fields) -> float:
    """How well worth naming one row is, from its own record: how many kinds
    of consequence its description reports, a recent eruption, and height."""
    text = str(record.get(fields["text"]) or "")
    score = float(len({m.lower() for m in _NOTABLE.findall(text)}))
    year = _number(record.get(fields["year"]))
    if year is not None and year >= 1900:
        score += 0.5
    size = _number(record.get(fields["size"]))
    if size is not None:
        score += min(max(size, 0.0), 4000.0) / 4000.0
    return round(score, 3)


def _numeric_share(records, column) -> float:
    values = [r.get(column) for r in records[:500]]
    return sum(_number(v) is not None for v in values) / max(1, len(values))


def _rows(records, roles, entry=None, fame=None):
    rows, bad = [], 0
    for i, record in enumerate(records):
        lon, lat = _number(record.get(roles["lon"])), _number(record.get(roles["lat"]))
        if lon is None or lat is None or not (-90 <= lat <= 90):
            bad += 1
            continue
        # Some catalogues write the Pacific past 180 (Fiji at 181.6 E).
        lon = (lon + 180.0) % 360.0 - 180.0
        row = {"i": i, "lon": round(lon, 5), "lat": round(lat, 5)}
        if "size" in roles:
            row["size"] = _number(record.get(roles["size"]))
        if "category" in roles:
            row["category"] = str(record.get(roles["category"]) or "")
            mapping = (entry or {}).get("category_map")
            if mapping:
                row["category"] = mapping.get(row["category"],
                                              entry.get("category_other") or "")
        if "group" in roles:
            row["group"] = " ".join(str(record.get(c) or "") for c in roles["group"])
        if "order" in roles:
            order = _number(record.get(roles["order"]))
            row["order"] = order if order is not None else i
        if "name" in roles:
            row["name"] = str(record.get(roles["name"]) or "")
        if roles.get("country"):
            row["country"] = str(record.get(roles["country"]) or "").upper()
        notability = (entry or {}).get("notability")
        if notability:
            row["notable"] = _notability(record, notability)
            if fame is not None:
                links = fame.get(str(record.get(entry["fame"]["id"]) or ""), 0)
                year = _number(record.get(notability["year"]))
                # Renown first; the record's own prose breaks ties.
                row["notable"] = round(
                    10.0 * _renown(links, year) + 0.1 * row["notable"], 3)
        rows.append(row)
    return rows, bad


def _category(rows, roles, entry):
    if "category" not in roles:
        return None
    values = []
    for row in rows:
        if row["category"] not in values:
            values.append(row["category"])
    order = entry.get("category_order")
    if order:
        values = [v for v in order if v in values] + [v for v in values if v not in order]
    if len(values) > MAX_CATEGORIES:
        for row in rows:
            row["category"] = None
        return None
    labels = dict(entry.get("category_labels") or {})
    if entry.get("category_counts"):
        counts = {v: sum(1 for r in rows if r["category"] == v) for v in values}
        labels = {v: f"{labels.get(v, v)} ({counts[v]:,})" for v in values}
    return {"field": roles["category"], "label": entry.get("category_label") or
            roles["category"],
            "classes": [{"value": v, "label": labels.get(v, v)} for v in values]}


def _km(a, b) -> float:
    la1, la2 = math.radians(a[1]), math.radians(b[1])
    d = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin(
        math.radians(b[0] - a[0]) / 2) ** 2
    return 2 * 6371.0088 * math.asin(math.sqrt(min(1.0, d)))


# Group ends this close together are the same place: Minard's three columns
# start together at the Niemen, and two of them end there.
SAME_PLACE_KM = 60.0
STORY_GROUPS = 10


def _flow_layer(base, rows, roles, entry, excluded):
    rows = [r for r in rows if r.get("size") is not None]
    category = _category(rows, roles, entry)
    if "group" not in roles:
        for r in rows:
            r["group"] = "1"
    if "order" not in roles:
        for r in rows:
            r["order"] = r["i"]
    groups: dict = {}
    for r in rows:
        groups.setdefault(r["group"], []).append(r)
    for g in groups.values():
        g.sort(key=lambda r: (r["order"], r["i"]))
    size_label = entry.get("size_label") or roles["size"]
    features = [{"group": r["group"], "order": r["order"], "lon": r["lon"],
                 "lat": r["lat"], "size": r["size"], "category": r.get("category")}
                for r in sorted(rows, key=lambda r: (str(r["group"]), r["order"], r["i"]))]
    stats = {"count": len(features), "groups": len(groups)}
    notes = []
    field = re.sub(r"[^a-z0-9_]+", "_", roles["size"].lower())
    if len(groups) <= STORY_GROUPS:
        story = _story(groups)
        stats.update({f"start_{field}": story["start"], f"turn_{field}": story["turn"],
                      f"end_{field}": story["end"]})
        templates = entry.get("notes") or {"start": "{v:,.0f}", "turn": "{v:,.0f}",
                                           "end": "{v:,.0f}"}
        for key in ("start", "turn", "end"):
            point = story[f"{key}_at"]
            notes.append({"text": templates[key].format(v=story[key]),
                          "lon": point["lon"], "lat": point["lat"],
                          "category": point.get("category")})
        template = entry.get("summary") or (
            "Band width is proportional to {label} · {start:,.0f} at the start, "
            "{turn:,.0f} at the farthest point, {end:,.0f} at the end")
        summary = template.format(label=size_label, **story)
    else:
        summary = f"{len(groups):,} tracks · band width is proportional to {size_label}"
    return {
        **base, "kind": "flow",
        "size": {"field": roles["size"], "label": size_label, "unit": "",
                 "scale": "linear"},
        "category": category, "features": features, "stats": stats,
        "notes": notes, "summary": summary,
        "river_names": dict(entry.get("river_names") or {}),
        "encoding": (f"Band width ∝ {size_label} (linear, as Minard drew it)"
                     + (f"; colour = {category['label'].lower()}" if category else "")
                     + f"; {len(groups)} group{'s' if len(groups) != 1 else ''}"),
        "excluded": excluded,
    }


def _story(groups: dict) -> dict:
    """Start, farthest point and end: the three numbers a flow map is read by.

    The main group is the one that starts largest. Start is every group's
    first row; the turn is the main group's row farthest from where it began;
    the end is every group that finishes where the main group does -- a column
    that merged back mid-route is not counted twice.
    """
    main = max(groups, key=lambda g: (groups[g][0]["size"], str(g)))
    path = groups[main]
    origin, finish = path[0], path[-1]
    turn = max(path, key=lambda r: (_km((origin["lon"], origin["lat"]),
                                        (r["lon"], r["lat"])), -r["order"]))
    start = sum(g[0]["size"] for g in groups.values()
                if _km((g[0]["lon"], g[0]["lat"]), (origin["lon"], origin["lat"]))
                <= SAME_PLACE_KM)
    end = sum(g[-1]["size"] for g in groups.values()
              if _km((g[-1]["lon"], g[-1]["lat"]), (finish["lon"], finish["lat"]))
              <= SAME_PLACE_KM)
    return {"start": start, "turn": turn["size"], "end": end,
            "start_at": origin, "turn_at": turn, "end_at": finish}


def _places_layer(base, rows, entry, excluded):
    major = set(entry.get("major") or ())
    features = [{"id": f"p{r['i']}", "lon": r["lon"], "lat": r["lat"],
                 "name": r.get("name") or "", "major": r.get("name") in major}
                for r in rows]
    return {**base, "kind": "places", "size": None, "category": None,
            "features": features, "stats": {"count": len(features)},
            "summary": f"{len(features)} places", "encoding": "Labelled points",
            "excluded": excluded}


def _in_window(lon, lat, window: Window) -> bool:
    w, s, e, n = window.bbox or window.extent
    return w <= lon <= e and s <= lat <= n


def _scope(rows: list, window: Window) -> tuple[list, list]:
    """Rows inside the map window and, when the map names countries in
    `within` and the data carries a country code, inside those countries.

    A box alone is not a scope: drawn around Europe it admits Ankara and
    Tehran, and Russia's box spans the globe. The country column is exact
    where polygons, simplified at 1:50m, put St Petersburg in the sea.
    """
    inside = [r for r in rows if _in_window(r["lon"], r["lat"], window)]
    excluded = []
    if len(rows) > len(inside):
        excluded.append({"count": len(rows) - len(inside),
                         "reason": "outside the map window"})
    codes = [c for c in window.within or () if "-" not in c and len(c) in (2, 3)]
    if codes and inside and "country" in inside[0]:
        from mapsvc.cartography import iso

        wanted = {iso.to_alpha2(c) for c in codes} - {None}
        kept = [r for r in inside if iso.to_alpha2(r["country"]) in wanted]
        if len(kept) < len(inside):
            excluded.append({"count": len(inside) - len(kept),
                             "reason": "in countries not on the map"})
        inside = kept
    return inside, excluded


def _points_layer(base, rows, roles, entry, excluded, window):
    inside, scoped = _scope(rows, window)
    excluded = excluded + scoped
    category = _category(inside, roles, entry)
    size = None
    if "size" in roles:
        size = {"field": roles["size"], "label": entry.get("size_label") or roles["size"],
                "unit": "", "scale": entry.get("scale") or "sqrt"}
    features = [{"id": f"r{r['i']}", "lon": r["lon"], "lat": r["lat"],
                 "size": r.get("size"), "category": r.get("category"),
                 "name": r.get("name"), "notable": r.get("notable")} for r in inside]
    size_text = (f"circle area ∝ {size['label']}" if size and size["scale"] != "magnitude"
                 else "circle area ∝ 10^(M/2)" if size else
                 f"colour = {category['label'].lower()}" if category else "one dot per row")
    noun = entry.get("noun") or "points"
    if category and entry.get("category_colours"):
        category["colours"] = dict(entry["category_colours"])
    summary = (f"{len(features):,} {noun} {entry['summary_note']} · {size_text}"
               if entry.get("summary_note") else f"{len(features):,} {noun} · {size_text}")
    return {**base, "kind": "points", "size": size, "category": category,
            **({"marker": entry["marker"]} if entry.get("marker") else {}),
            "features": features, "stats": {"count": len(features)},
            "summary": summary,
            "encoding": (size_text[:1].upper() + size_text[1:]
                         + (f". {entry['note']}" if entry.get("note") else "")),
            "excluded": excluded,
            "label_top": entry.get("label_top", 5 if "name" in roles else 0),
            "noun": noun}


def _caveats(entry, kept) -> str:
    """". Caveat: Moscow is an older figure ...; ..." for the cities drawn."""
    found: dict = {}
    for row in kept:
        text = (entry.get("caveats") or {}).get(row.get("name"))
        if text:
            found.setdefault(text, []).append(row["name"])
    if not found:
        return ""
    parts = [(" and ".join(names) + (" is " if len(names) == 1 else " are ") + text)
             for text, names in found.items()]
    return "; caveat: " + "; ".join(parts)


def _country_name(alpha2: str) -> str:
    """Natural Earth's name for an ISO alpha-2 code; the code if unknown."""
    try:
        from mapsvc import harvest
        from mapsvc.cartography import iso

        for feature in harvest.context_countries():
            props = feature.get("properties") or {}
            for key in ("ISO_A2_EH", "ISO_A2"):
                if str(props.get(key) or "").upper() == alpha2.upper():
                    return str(props.get("NAME") or alpha2)
    except Exception:       # a name is a courtesy
        pass
    return alpha2


def _left_out_by_scope(rows: list, window: Window, floor: float, measure: str) -> str:
    """"; the largest city in a country not drawn is Istanbul (Turkey, 15.7M),
    so it is not ranked" -- said when a country list leaves out something that
    would have made the ranking, so the definition of the region is not
    silent."""
    codes = [c for c in window.within or () if "-" not in c and len(c) in (2, 3)]
    if not codes or not rows or "country" not in rows[0]:
        return ""
    from mapsvc.cartography import iso

    wanted = {iso.to_alpha2(c) for c in codes} - {None}
    outside = [r for r in rows if _in_window(r["lon"], r["lat"], window)
               and iso.to_alpha2(r["country"]) not in wanted
               and r.get("size") is not None and r["size"] >= floor]
    if not outside:
        return ""
    big = max(outside, key=lambda r: (r["size"], -r["i"]))
    figure = _short_number(big["size"])
    return (f"; region as drawn: {big['name']} ({_country_name(big['country'])}, "
            f"{figure}) is in the window but its country is not in the "
            f"region, so it is not ranked")


def _short_number(v: float) -> str:
    return f"{v / 1e6:.1f} million" if v >= 1e6 else f"{v:,.0f}"


def _ranked_layer(base, rows, roles, entry, excluded, window, n):
    """The N largest by size, within the window and `within`: every one is
    named on the map with its value, so the ranking can be read off it."""
    inside, scoped = _scope(rows, window)
    excluded = excluded + scoped
    sized = [r for r in inside if r.get("size") is not None and r["size"] > 0]
    if len(sized) < len(inside):
        excluded.append({"count": len(inside) - len(sized), "reason": "without a size"})
    if not sized:
        raise OverlayError(f"{base['provenance']['url']} has no sized rows inside "
                           "the map window", "overlays.id")
    sized.sort(key=lambda r: (-r["size"], r["i"]))
    kept = sized[:n]
    if len(sized) > n:
        excluded.append({"count": len(sized) - n,
                         "reason": f"smaller than the {n} largest"})
    size_label = entry.get("size_label") or roles["size"]
    noun = entry.get("rank_noun") or "places"
    where = f" in {window.name}" if window.name else ""
    measure = re.split(r"\s*[(,]", size_label)[0].lower()
    features = [{"id": f"r{r['i']}", "lon": r["lon"], "lat": r["lat"],
                 "size": r["size"], "category": None, "name": r.get("name") or "",
                 "rank": rank} for rank, r in enumerate(kept, 1)]
    scope = ("the map window and the countries drawn"
             if any(e["reason"].startswith("in countries") for e in scoped)
             or (window.within and "country" in roles) else "the map window")
    definition = entry.get("definition") or size_label
    left_out = _left_out_by_scope(rows, window, kept[-1]["size"], measure)
    return {**base, "kind": "points", "rank": True,
            "title": f"The {len(kept)} largest {noun}{where} by {measure}",
            "size": {"field": roles["size"], "label": size_label, "unit": "",
                     "scale": "sqrt"},
            "category": None, "features": features,
            "stats": {"count": len(features), "largest": kept[0]["size"],
                      "smallest": kept[-1]["size"]},
            "summary": f"{definition[:1].upper()}{definition[1:]}",
            "encoding": (f"{measure[:1].upper()}{measure[1:]}: {definition}. "
                         f"Circle area ∝ {measure}; ranked within {scope}; every "
                         f"one labelled with its {measure}{_caveats(entry, kept)}"
                         f"{left_out}"),
            "excluded": excluded, "label_top": len(features), "noun": noun}


# ---------------------------------------------------------------- ibtracs ---

# IBTrACS USA_SSHS: -5 unknown, -4 post-tropical, -3 disturbance, -2
# subtropical, -1 tropical depression, 0 tropical storm, 1-5 hurricane category.
SSHS_CLASSES = (
    ("other", "Before genesis or post-tropical"),
    ("td", "Tropical depression (< 34 kt)"), ("ts", "Tropical storm (34–63 kt)"),
    ("cat1", "Category 1 (64–82 kt)"), ("cat2", "Category 2 (83–95 kt)"),
    ("cat3", "Category 3 (96–112 kt)"), ("cat4", "Category 4 (113–136 kt)"),
    ("cat5", "Category 5 (≥ 137 kt)"))
# Saffir-Simpson thresholds in knots, for positions without a USA_SSHS.
SSHS_KT = ((137, 5), (113, 4), (96, 3), (83, 2), (64, 1), (34, 0), (0, -1))


def _sshs(cells: dict) -> int | None:
    code = _number(cells.get("USA_SSHS"))
    if code is not None:
        code = int(code)
        if code == -2:          # subtropical: depression or storm by its wind
            wind = _number(cells.get("USA_WIND")) or 0
            return 0 if wind >= 34 else -1
        return code
    wind = _number(cells.get("USA_WIND"))
    if wind is None:
        wind = _number(cells.get("WMO_WIND"))
    if wind is None:
        return None
    return next(c for kt, c in SSHS_KT if wind >= kt)


def _sshs_class(code: int | None) -> str:
    if code is None or code < -1:
        return "other"
    return {-1: "td", 0: "ts"}.get(code, f"cat{code}")


def _landfall_region(lon: float, lat: float) -> str:
    """The first-level division a landfall is in ("Texas"; "Quintana Roo,
    Mexico"), from Natural Earth's admin-1 polygons: the one that holds the
    point, else the nearest within 40 km (the fix is rounded to 0.1 degree and
    often sits just off the coast). Empty when that data cannot be had."""
    try:
        from mapsvc import project
        from mapsvc.harvest import load_source

        features = load_source("admin_1")["features"]
    except Exception:       # a name is a courtesy; the landfall stands without it
        return ""
    best, best_km = None, 40.0
    for feature in features:
        geometry = feature.get("geometry") or {}
        polygons = (geometry.get("coordinates") or [] if geometry.get("type") == "MultiPolygon"
                    else [geometry.get("coordinates") or []])
        for polygon in polygons:
            if not polygon:
                continue
            ring = polygon[0]
            xs = [p[0] for p in ring]
            ys = [p[1] for p in ring]
            if not (min(xs) - 0.5 <= lon <= max(xs) + 0.5 and min(ys) - 0.5 <= lat <= max(ys) + 0.5):
                continue
            if project.point_in_ring(lon, lat, [tuple(p[:2]) for p in ring]):
                best, best_km = feature, 0.0
                break
            near = min(math.hypot((p[0] - lon) * math.cos(math.radians(lat)), p[1] - lat)
                       for p in ring) * 111.2
            if near < best_km:
                best, best_km = feature, near
        if best_km == 0.0:
            break
    if best is None:
        return ""
    props = best.get("properties") or {}
    name, country = props.get("name"), props.get("admin") or ""
    if not name:
        return ""
    if country in ("United States of America", ""):
        return name
    # "Quintana Roo, Mexico", but not "Carriacou and Petite Martinique, Grenada".
    return f"{name}, {country}" if len(name) + len(country) <= 26 else country


def _ibtracs_layer(base: dict, text: str, filters: dict, window: Window) -> dict:
    """One line per storm of a season, its category at every position."""
    season = str(filters["season"])
    lines = text.splitlines()
    header = next(csv.reader(lines[:1]), [])
    if "SID" not in header or "SEASON" not in header:
        raise OverlayError(f"{base['provenance']['url']} is not an IBTrACS CSV",
                           "overlays.id")
    column = header.index("SEASON")
    # A season is a few hundred of 128,000 rows: test the column before parsing.
    picked = [line for line in lines[2:] if line.split(",", column + 1)[column] == season]
    storms: dict = {}
    spur = 0
    for cells in csv.reader(picked):
        row = dict(zip(header, (c.strip() for c in cells)))
        if row.get("TRACK_TYPE") != "main":
            spur += 1
            continue
        storms.setdefault(row["SID"], []).append(row)
    if not storms:
        raise OverlayError(f"IBTrACS has no storms in season {season}", "overlays.id")
    features, never, provisional = [], 0, 0
    has_records = "USA_RECORD" in header
    for sid in sorted(storms):
        rows = sorted(storms[sid], key=lambda r: r["ISO_TIME"])
        codes = [_sshs(r) for r in rows]
        if max((c for c in codes if c is not None), default=-5) < -1:
            never += 1
            continue
        if not any(r.get("USA_AGENCY", "").startswith("hurdat") for r in rows):
            provisional += 1
        coords, classes, winds, landfalls = [], [], [], []
        over_land = False
        for r, code in zip(rows, codes):
            lon, lat = _number(r.get("LON")), _number(r.get("LAT"))
            if lon is None or lat is None:
                continue
            # IBTrACS LANDFALL is the distance to land (km) until the next
            # position: 0 where the centre makes landfall. The first position
            # of each run of zeros is the landfall; a hurricane's is marked.
            at_land = r.get("LANDFALL") == "0"
            if has_records:
                # NHC's own landfall records (HURDAT2 type "L"): exact fix and
                # time, and they include a crossing of an island (Beryl over
                # Carriacou) that the 3-hourly distance-to-land never reads 0.
                is_landfall = r.get("USA_RECORD") == "L"
            else:
                is_landfall = at_land and not over_land
            if is_landfall and code is not None and code >= 1:
                landfalls.append([round(lon, 3), round(lat, 3), code,
                                  r["ISO_TIME"][:10],
                                  _number(r.get("USA_WIND")), ""])
            over_land = at_land
            coords.append([round(lon, 3), round(lat, 3)])
            classes.append(_sshs_class(code))
            wind = _number(r.get("USA_WIND"))
            winds.append(wind if wind is not None else (_number(r.get("WMO_WIND")) or 0))
        if len(coords) < 2:
            never += 1
            continue
        peak = max(range(len(coords)), key=lambda i: (winds[i], -i))
        top = max((c for c in codes if c is not None), default=-1)
        name = rows[0].get("NAME") or "UNNAMED"
        features.append({
            "id": sid, "coords": coords, "classes": classes[:-1],
            "category": _sshs_class(top), "name": name.title(),
            "peak": top, "peak_wind": winds[peak], "peak_at": coords[peak],
            "landfall": any(r.get("LANDFALL") == "0" for r in rows),
            "landfalls": landfalls,
        })
    for f in features:
        for landfall in f["landfalls"]:
            landfall[5] = _landfall_region(landfall[0], landfall[1])
    hurricanes = sum(f["peak"] >= 1 for f in features)
    major = sum(f["peak"] >= 3 for f in features)
    excluded = []
    if never:
        excluded.append({"count": never, "reason": "never a tropical or "
                         "subtropical cyclone (disturbances, invests)"})
    if spur:
        excluded.append({"count": spur, "reason": "positions on alternative (spur) tracks"})
    basin = filters.get("basin")
    basin_name = IBTRACS_BASINS.get(basin, "North Atlantic")
    vintage = f"{basin_name} basin; season {season}"
    if provisional:
        vintage += (f"; {provisional} storm{'s' if provisional != 1 else ''} "
                    "provisional (operational data, not yet reanalysed)")
    base["provenance"]["vintage"] = vintage
    present = {c for f in features for c in f["classes"]}
    return {**base, "kind": "lines", "along": True,
            "title": f"{basin_name} tropical cyclones, {season}",
            "label": "Storm tracks",
            "size": None,
            "category": {"field": "USA_SSHS", "label": "Saffir-Simpson category",
                         "ordered": True,
                         "classes": [{"value": v, "label": label}
                                     for v, label in SSHS_CLASSES if v in present]},
            "features": features,
            "stats": {"count": len(features), "hurricanes": hurricanes,
                      "major_hurricanes": major},
            "summary": (f"{len(features)} tropical cyclones · "
                        f"{hurricanes} hurricanes, {major} major"),
            "encoding": ("One line per storm, coloured by its Saffir-Simpson "
                         "category at each three-hourly position (US agency "
                         "1-minute sustained wind); each storm named beside its track, hurricane "
                         "landfalls marked (NHC landfall records, UTC dates; those of "
                         "major hurricanes described)"),
            "excluded": excluded, "noun": "storms"}


def _segments(records, roles) -> list:
    """Rows with a start and an end coordinate, as one-segment lines."""
    if not ("end_lon" in roles and "end_lat" in roles and "lon" in roles and "lat" in roles):
        return []
    out = []
    for i, r in enumerate(records):
        pts = [_number(r.get(roles[k])) for k in ("lon", "lat", "end_lon", "end_lat")]
        if None in pts or not all(-180 <= pts[j] <= 180 and -90 <= pts[j + 1] <= 90
                                  for j in (0, 2)):
            continue
        out.append((str(i), r, [(pts[0], pts[1]), (pts[2], pts[3])]))
    return out


def _natural(value):
    digits = re.findall(r"\d+", str(value))
    return (0, int(digits[0]), str(value)) if digits else (1, 0, str(value))


def _lines_layer(base, lines, roles, window):
    features = []
    column = roles.get("category")
    for fid, props, coords in lines:
        pts = [[float(c[0]), float(c[1])] for c in coords if len(c) >= 2]
        if len(pts) >= 2:
            value = props.get(column) if column else None
            features.append({"id": fid, "coords": pts,
                             "category": None if value in (None, "") else str(value)})
    category = None
    values = sorted({f["category"] for f in features if f["category"]}, key=_natural)
    if column and 1 < len(values) <= MAX_CATEGORIES:
        category = {"field": column, "label": column, "ordered": True,
                    "classes": [{"value": v, "label": v} for v in values]}
    else:
        for f in features:
            f["category"] = None
    return {**base, "kind": "lines", "size": None, "category": category,
            "features": features, "stats": {"count": len(features)},
            "summary": f"{len(features):,} lines", "encoding": "One line per feature"
            + (f", coloured by {column}" if category else ""),
            "excluded": []}
