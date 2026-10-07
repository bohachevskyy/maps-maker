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

from mapsvc import fetch
from mapsvc.overlays import UNKNOWN_LICENSE, Capabilities, OverlayError, Window

MAX_BYTES = 25 * 1024 * 1024
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
    id_help="an https URL of a CSV (with lon/lat columns) or GeoJSON file",
    searchable=True,
)

_RDATASETS = "https://raw.githubusercontent.com/vincentarelbundock/Rdatasets/master/csv/"
_NE = "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/"
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
        "notes": {"start": "{v:,.0f} cross the Niemen", "turn": "{v:,.0f} reach Moscow",
                  "end": "≈ {v:,.0f} return"},
        "license": "public domain (1869 figures; transcription distributed in R HistData)",
        "attribution": _MINARD,
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
        "url": _RDATASETS + "dplyr/storms.csv",
        "title": "Atlantic hurricanes and tropical storms, 1975–2022 (NOAA HURDAT2)",
        "description": "Six-hourly positions of Atlantic storms with wind speed "
                       "and status: storm tracks as flows, width by wind.",
        "keywords": "hurricane storm tropical cyclone atlantic track wind noaa hurdat",
        "roles": {"lon": "long", "lat": "lat", "size": "wind", "category": "status",
                  "group": ("name", "year"), "order": "rownames"},
        "size_label": "maximum sustained wind (knots)", "category_label": "Status",
        "label": "Storm tracks",
        "license": "public domain (NOAA); packaged in R dplyr (MIT)",
        "attribution": "NOAA National Hurricane Center HURDAT2, via R dplyr and Rdatasets",
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
        "roles": {"size": "pop_max", "name": "name"},
        "size_label": "Population", "label": "Cities",
        "license": "public domain", "attribution": "Natural Earth",
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


# ------------------------------------------------------------------ parse ---

def parse(overlay_id: str) -> str:
    """An https URL, refused if it could reach this machine or its network."""
    url = overlay_id.strip()
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
        parse(newurl)
        _check_resolves_public(urllib.parse.urlsplit(newurl).hostname)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _download(url: str) -> bytes:
    parse(url)
    _check_resolves_public(urllib.parse.urlsplit(url).hostname)
    opener = urllib.request.build_opener(_SafeRedirect)
    request = urllib.request.Request(url, headers={"User-Agent": fetch.USER_AGENT})
    try:
        with opener.open(request, timeout=TIMEOUT) as response:
            payload = response.read(MAX_BYTES + 1)
    except urllib.error.HTTPError as exc:
        raise OverlayError(f"{url} returned HTTP {exc.code}", "overlays.id") from exc
    except (OSError, http.client.HTTPException) as exc:
        raise OverlayError(f"could not fetch {url}: {exc}", "overlays.id") from exc
    if len(payload) > MAX_BYTES:
        raise OverlayError(f"{url} is larger than {MAX_BYTES // 2**20} MB",
                           "overlays.id")
    return payload


def fetch_bytes(url: str) -> bytes:
    digest = hashlib.sha256(url.encode()).hexdigest()[:16]
    target = fetch.path("overlays", "web", digest)
    if target.exists():
        return target.read_bytes()
    payload = _download(url)
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

    def as_prompt_line(self) -> str:
        return f"{self.id}: {self.title} -- {self.subtitle} [{self.license}]"


def search(query: str, limit: int = 8) -> list:
    words = set(re.findall(r"[a-z0-9]+", query.lower()))
    scored = []
    for i, entry in enumerate(CATALOGUE):
        haystack = set(re.findall(r"[a-z0-9]+", " ".join(
            (entry["title"], entry["description"], entry["keywords"])).lower()))
        score = len(words & haystack)
        if score:
            scored.append((-score, i, entry))
    scored.sort(key=lambda t: (t[0], t[1]))
    return [Candidate(entry) for _, _, entry in scored[:limit]]


# ------------------------------------------------------------------- load ---

_LON = ("lon", "long", "longitude", "lng", "x")
_LAT = ("lat", "latitude", "y")
_SIZE = ("survivors", "value", "count", "size", "population", "pop", "pop_max",
         "magnitude", "mag", "amount", "total", "n", "wind")
_CATEGORY = ("direction", "category", "type", "class", "kind", "status")
_GROUP = ("group", "series", "track", "route", "trip")
_ORDER = ("order", "seq", "sequence", "step", "index")
_NAME = ("name", "city", "label", "place", "title", "town")


def check(url: str) -> None:
    """Fetch and shape-check a URL without a window; raises OverlayError."""
    world = Window(None, None, (-180.0, -90.0, 180.0, 90.0))
    layer_from_bytes(fetch_bytes(url), url, world)


def load(overlay_id: str, window: Window) -> dict:
    payload = fetch_bytes(overlay_id)
    return layer_from_bytes(payload, overlay_id, window)


def layer_from_bytes(payload: bytes, url: str, window: Window) -> dict:
    entry = _BY_URL.get(url, {})
    text = payload.decode("utf-8-sig", errors="replace")
    if text.lstrip().startswith("{"):
        records, columns, lines = _geojson(text, url)
    else:
        records, columns = _csv(text, url)
        lines = []
    roles = dict(entry.get("roles") or _guess_roles(columns, records))
    host = urllib.parse.urlsplit(url).hostname
    provenance = {
        "name": entry.get("attribution") or host,
        "attribution": entry.get("attribution") or host,
        "license": entry.get("license") or UNKNOWN_LICENSE,
        "url": url,
        "vintage": None,
    }
    base = {"source": "web", "id": url, "provenance": provenance,
            "title": entry.get("title") or f"Data from {host}",
            "label": entry.get("label") or entry.get("title") or host}
    if lines:
        return _lines_layer(base, lines, roles, window)
    if "lon" not in roles or "lat" not in roles:
        raise OverlayError(
            f"{url} has no recognisable longitude/latitude columns (found "
            f"{', '.join(columns[:12])})", "overlays.id")
    rows, bad = _rows(records, roles)
    if not rows:
        raise OverlayError(f"{url} has no rows with numeric coordinates", "overlays.id")
    if len(rows) > MAX_ROWS:
        raise OverlayError(f"{url} has {len(rows):,} rows, over {MAX_ROWS:,}",
                           "overlays.id")
    excluded = [{"count": bad, "reason": "rows without numeric coordinates"}] if bad else []
    has_size = "size" in roles
    if has_size and ("group" in roles or "order" in roles):
        return _flow_layer(base, rows, roles, entry, excluded)
    if not has_size and "name" in roles and len(rows) <= MAX_PLACES:
        return _places_layer(base, rows, entry, excluded)
    return _points_layer(base, rows, roles, entry, excluded, window)


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


def _numeric_share(records, column) -> float:
    values = [r.get(column) for r in records[:500]]
    return sum(_number(v) is not None for v in values) / max(1, len(values))


def _rows(records, roles):
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
        if "group" in roles:
            row["group"] = " ".join(str(record.get(c) or "") for c in roles["group"])
        if "order" in roles:
            order = _number(record.get(roles["order"]))
            row["order"] = order if order is not None else i
        if "name" in roles:
            row["name"] = str(record.get(roles["name"]) or "")
        rows.append(row)
    return rows, bad


def _category(rows, roles, entry):
    if "category" not in roles:
        return None
    values = []
    for row in rows:
        if row["category"] not in values:
            values.append(row["category"])
    if len(values) > MAX_CATEGORIES:
        for row in rows:
            row["category"] = None
        return None
    labels = entry.get("category_labels") or {}
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


def _points_layer(base, rows, roles, entry, excluded, window):
    inside = [r for r in rows if _in_window(r["lon"], r["lat"], window)]
    outside = len(rows) - len(inside)
    if outside:
        excluded = excluded + [{"count": outside, "reason": "outside the map window"}]
    category = _category(inside, roles, entry)
    size = None
    if "size" in roles:
        size = {"field": roles["size"], "label": entry.get("size_label") or roles["size"],
                "unit": "", "scale": entry.get("scale") or "sqrt"}
    features = [{"id": f"r{r['i']}", "lon": r["lon"], "lat": r["lat"],
                 "size": r.get("size"), "category": r.get("category"),
                 "name": r.get("name")} for r in inside]
    size_text = (f"circle area ∝ {size['label']}" if size and size["scale"] != "magnitude"
                 else "circle area ∝ 10^(M/2)" if size else "one dot per row")
    return {**base, "kind": "points", "size": size, "category": category,
            "features": features, "stats": {"count": len(features)},
            "summary": f"{len(features):,} points · {size_text}",
            "encoding": size_text[:1].upper() + size_text[1:], "excluded": excluded,
            "label_top": 5 if "name" in roles else 0}


def _lines_layer(base, lines, roles, window):
    features = []
    for fid, props, coords in lines:
        pts = [[float(c[0]), float(c[1])] for c in coords if len(c) >= 2]
        if len(pts) >= 2:
            features.append({"id": fid, "coords": pts, "category": None})
    return {**base, "kind": "lines", "size": None, "category": None,
            "features": features, "stats": {"count": len(features)},
            "summary": f"{len(features):,} lines", "encoding": "One line per feature",
            "excluded": []}
