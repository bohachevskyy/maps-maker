"""OpenStreetMap features via the Overpass API (ODbL).

The id is a tag filter in a deliberately small grammar -- `railway=rail;
highspeed=yes`: key=value pairs, ANDed, lower-case [a-z0-9_:], `*` meaning
"any value". The service writes the Overpass QL; the model never does, so it
cannot ask for the planet or run arbitrary queries on a public server.

The area is the manifest's: `within` becomes Overpass areas by ISO 3166 code
(the country itself, so a filter inside France does not wander into the
Channel Tunnel's English half), a bbox becomes a box filter, both narrow
twice.

Measured 2026-10-07 on overpass-api.de: France, railway=rail + highspeed=yes,
area query 145 s and 11 MB; the same as a bbox 108 s and 17 MB (neighbours
included). The server-side timeout is 400 s because 180 s was exceeded at 01:35 local
that night. Queries this slow are the reason every response is cached on disk,
keyed by the exact QL, and why a second public endpoint is tried when the
first refuses (504 "too busy" was common that night). overpass-api.de answers
406 to a request without a User-Agent.
"""

import hashlib
import json
import math
import os
import re
import time
import urllib.parse

from mapsvc import fetch
from mapsvc.overlays import Capabilities, OverlayError, Window, geo

ENDPOINTS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
)
TIMEOUT_S = 400
DEFAULT_BUDGET_S = 300
STATUS_TIMEOUT_S = 2
MAXSIZE = 512 * 1024 * 1024
MAX_WAYS = 40_000
MAX_PAIRS = 4

CAPABILITIES = Capabilities(
    source="overpass",
    license="ODbL 1.0",
    attribution="© OpenStreetMap contributors",
    description="any OpenStreetMap feature selected by tags -- railways, "
                "motorways, rivers, power lines, stations -- inside the map's "
                "within/bbox, drawn as lines or points. Slow when uncached "
                "(one to three minutes for a country), then instant",
    id_help="semicolon-separated key=value tag pairs, ANDed, lower case "
            "[a-z0-9_:], value * for any, e.g. 'railway=rail;usage=main' "
            "or 'power=line'",
)

_TOKEN = re.compile(r"[a-z0-9_:]+")
_VALUE = re.compile(r"[a-z0-9_:.\-]+|\*")


def parse(overlay_id: str) -> str:
    return ";".join(f"{k}={v}" for k, v in _pairs(overlay_id))


def _pairs(overlay_id: str) -> list[tuple[str, str]]:
    pairs = []
    for part in overlay_id.split(";"):
        part = part.strip()
        if not part:
            continue
        if part.count("=") != 1:
            raise OverlayError(f"overpass filter part {part!r} is not key=value; "
                               f"{CAPABILITIES.id_help}", "overlays.id")
        key, value = (s.strip() for s in part.split("="))
        if not _TOKEN.fullmatch(key) or not _VALUE.fullmatch(value):
            raise OverlayError(
                f"overpass filter {part!r} uses characters outside [a-z0-9_:]; "
                "free Overpass QL is not accepted", "overlays.id")
        if any(k == key for k, _ in pairs):
            raise OverlayError(f"overpass filter repeats {key!r}", "overlays.id")
        pairs.append((key, value))
    if not pairs:
        raise OverlayError(f"overpass filter is empty; {CAPABILITIES.id_help}",
                           "overlays.id")
    if len(pairs) > MAX_PAIRS:
        raise OverlayError(f"overpass filter has {len(pairs)} pairs, at most "
                           f"{MAX_PAIRS}", "overlays.id")
    if all(v == "*" for _, v in pairs):
        raise OverlayError("overpass filter needs at least one concrete value; "
                           f"'{pairs[0][0]}=*' alone matches far too much",
                           "overlays.id")
    return pairs


def query(pairs, window: Window) -> str:
    """The Overpass QL for a filter and a window. Pure, so it is testable."""
    tags = "".join(f'["{k}"]' if v == "*" else f'["{k}"="{v}"]' for k, v in pairs)
    areas = []
    for code in window.within or ():
        if "-" in code:
            areas.append(f'area["ISO3166-2"="{code}"]')
        else:
            two = _alpha2(code)
            areas.append(f'area["ISO3166-1"="{two}"][admin_level=2]')
    scope = ""
    head = f"[out:json][timeout:{TIMEOUT_S}][maxsize:{MAXSIZE}];"
    if areas:
        head += ("(" + ";".join(areas) + ";)->.a;" if len(areas) > 1
                 else areas[0] + "->.a;")
        scope += "(area.a)"
    if window.bbox is not None:
        w, s, e, n = window.bbox
        scope += f"({s:g},{w:g},{n:g},{e:g})"
    if not scope:
        raise OverlayError("an overpass overlay needs a within or a bbox", "overlays")
    return head + f"(way{tags}{scope};node{tags}{scope};);out tags geom;"


def _alpha2(code: str) -> str:
    from mapsvc.cartography import iso

    two = iso.to_alpha2(code)
    if not two or not re.fullmatch(r"[A-Z]{2}", two):
        raise OverlayError(f"no ISO 3166-1 code for {code!r}; Overpass areas are "
                           "found by ISO code", "within")
    return two


def load(overlay_id: str, window: Window) -> dict:
    pairs = _pairs(overlay_id)
    ql = query(pairs, window)
    digest = hashlib.sha256(ql.encode()).hexdigest()[:16]
    target = fetch.path("overlays", "overpass", f"{digest}.json")
    elapsed = None
    if target.exists():
        payload = target.read_bytes()
    else:
        started = time.monotonic()
        payload = _post(ql)
        elapsed = time.monotonic() - started
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.name + ".part")
        temporary.write_bytes(payload)
        temporary.replace(target)
    data = json.loads(payload)
    layer = layer_from_overpass(data, pairs, window, ql)
    if elapsed is not None:
        layer["fetch_seconds"] = round(elapsed, 1)
    return layer


def _post(ql: str) -> bytes:
    body = urllib.parse.urlencode({"data": ql}).encode()
    budget = _budget_s()
    deadline = time.monotonic() + budget
    endpoints, errors = _ordered_endpoints(deadline)
    for endpoint in endpoints:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        started = time.monotonic()
        try:
            payload = fetch.post(endpoint, body, timeout=min(TIMEOUT_S, remaining))
        except fetch.FetchError as exc:
            elapsed = time.monotonic() - started
            outcome = f"status {exc.status}" if exc.status is not None else str(exc)[:120]
            errors.append(f"{endpoint}: {outcome}, {elapsed:.1f}s")
            continue
        elapsed = time.monotonic() - started
        if time.monotonic() >= deadline:
            errors.append(f"{endpoint}: budget expired, {elapsed:.1f}s")
            break
        if not payload.lstrip().startswith(b"{"):
            errors.append(f"{endpoint}: non-JSON answer, {elapsed:.1f}s")
            continue
        remark = json.loads(payload).get("remark") or ""
        if "runtime error" in remark:
            if "out of memory" in remark or "Query run out" in remark:
                raise OverlayError(
                    "that OpenStreetMap filter matches too much data for this "
                    "window; narrow the filter or the area", "overlays.id")
            errors.append(f"{endpoint}: {remark[:120]}, {elapsed:.1f}s")
            continue
        return payload
    if time.monotonic() >= deadline:
        raise OverlayError(
            f"Overpass wall-clock budget of {budget:g}s exhausted; endpoints tried: "
            + " | ".join(errors))
    raise OverlayError("every Overpass endpoint failed: " + " | ".join(errors))


def _budget_s() -> float:
    raw = os.environ.get("MAPSVC_OVERPASS_BUDGET", str(DEFAULT_BUDGET_S))
    try:
        budget = float(raw)
    except ValueError as exc:
        raise OverlayError("MAPSVC_OVERPASS_BUDGET must be a positive number") from exc
    if not math.isfinite(budget) or budget <= 0:
        raise OverlayError("MAPSVC_OVERPASS_BUDGET must be a positive number")
    return budget


def _ordered_endpoints(deadline: float) -> tuple[list[str], list[str]]:
    """Put endpoints reporting free slots first; omit explicit zero-slot ones."""
    available = []
    unknown = []
    outcomes = []
    for endpoint in ENDPOINTS:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        started = time.monotonic()
        status_url = endpoint.rsplit("/", 1)[0] + "/status"
        try:
            payload = fetch.get(status_url, timeout=min(STATUS_TIMEOUT_S, remaining))
        except fetch.FetchError as exc:
            elapsed = time.monotonic() - started
            outcome = f"status {exc.status}" if exc.status is not None else str(exc)[:100]
            outcomes.append(
                f"{endpoint}: status probe {outcome}, {elapsed:.1f}s")
            unknown.append(endpoint)
            continue
        elapsed = time.monotonic() - started
        match = re.search(rb"(\d+)\s+slots? available now", payload, re.IGNORECASE)
        if match and int(match.group(1)) == 0:
            outcomes.append(f"{endpoint}: no free slots, probe {elapsed:.1f}s")
        elif match and int(match.group(1)) > 0:
            available.append(endpoint)
        elif b"slot available after" in payload.lower():
            outcomes.append(f"{endpoint}: no free slots, probe {elapsed:.1f}s")
        else:
            unknown.append(endpoint)
    return available + unknown, outcomes


# Descriptions for the commonest filters; anything else is described by its
# tags, which is accurate if plain.
_NOUNS = {
    ("railway", "rail"): "railway lines", ("railway", "subway"): "metro lines",
    ("railway", "light_rail"): "light-rail lines", ("railway", "tram"): "tram lines",
    ("railway", "station"): "railway stations",
    ("highway", "motorway"): "motorways", ("highway", "trunk"): "trunk roads",
    ("waterway", "river"): "rivers", ("waterway", "canal"): "canals",
    ("power", "line"): "power lines", ("aeroway", "aerodrome"): "airports",
    ("amenity", "hospital"): "hospitals",
}
_MODIFIERS = {("highspeed", "yes"): "high-speed", ("electrified", "contact_line"): "electrified",
              ("usage", "main"): "main"}


def describe(pairs) -> str:
    nouns = [_NOUNS[p] for p in pairs if p in _NOUNS]
    mods = [_MODIFIERS[p] for p in pairs if p in _MODIFIERS]
    if nouns and len(nouns) + len(mods) == len(pairs):
        text = " ".join(mods + [nouns[0]])
        return text[:1].upper() + text[1:]
    return "OpenStreetMap features tagged " + " + ".join(f"{k}={v}" for k, v in pairs)


def _speed(tags: dict) -> float | None:
    raw = str(tags.get("maxspeed") or "")
    match = re.match(r"\s*(\d+(?:\.\d+)?)\s*(mph)?", raw)
    if not match:
        return None
    value = float(match.group(1))
    return value * 1.609344 if match.group(2) else value


SPEED_CLASSES = (
    {"value": "300+", "label": "300 km/h and above"},
    {"value": "200-299", "label": "200–299 km/h"},
    {"value": "<200", "label": "Below 200 km/h"},
    {"value": "untagged", "label": "No maxspeed tag"},
)


def _speed_class(speed: float | None) -> str:
    if speed is None:
        return "untagged"
    return "300+" if speed >= 300 else "200-299" if speed >= 200 else "<200"


def layer_from_overpass(data: dict, pairs, window: Window, ql: str) -> dict:
    elements = data.get("elements") or []
    ways = [e for e in elements if e.get("type") == "way" and len(e.get("geometry") or []) >= 2]
    nodes = [e for e in elements if e.get("type") == "node" and "lat" in e]
    vintage = (data.get("osm3s") or {}).get("timestamp_osm_base")
    what = describe(pairs)
    where = f" in {window.name}" if window.name else ""
    provenance = {
        "name": "OpenStreetMap",
        "attribution": CAPABILITIES.attribution + ", via the Overpass API",
        "license": CAPABILITIES.license,
        "url": None,
        "vintage": ((f"data as of {vintage[:10]}: " if vintage else "")
                    + "every element tagged "
                    + " + ".join(f"{k}={v}" for k, v in pairs)
                    + (f" in {window.name}" if window.name else " in the map window")),
    }

    if not ways:
        if len(nodes) > MAX_WAYS:
            raise OverlayError(f"{len(nodes):,} OpenStreetMap points match; narrow "
                               "the filter or the window", "overlays.id")
        features = [{"id": f"n{n['id']}", "lon": round(n["lon"], 5), "lat": round(n["lat"], 5),
                     "size": None, "category": None,
                     "name": (n.get("tags") or {}).get("name")} for n in nodes]
        features.sort(key=lambda f: f["id"])
        return {
            "source": "overpass", "id": parse(";".join(f"{k}={v}" for k, v in pairs)),
            "kind": "points", "title": what + where, "label": what,
            "size": None, "category": None, "features": features,
            "stats": {"count": len(features)},
            "summary": f"{len(features):,} in OpenStreetMap",
            "encoding": "One dot per OSM node", "excluded": [],
            "provenance": provenance,
        }

    if len(ways) > MAX_WAYS:
        raise OverlayError(f"{len(ways):,} OpenStreetMap ways match; narrow the "
                           "filter or the window", "overlays.id")
    ways.sort(key=lambda e: e["id"])
    railway = any(k == "railway" for k, _ in pairs)
    kept, service = [], []
    for way in ways:
        tags = way.get("tags") or {}
        # On a railway, `service` marks sidings, yards and crossovers: track,
        # but not route. Left in they add length and clutter, not network.
        (service if railway and tags.get("service") else kept).append(way)

    track_m = 0.0
    lines = []
    features = []
    speeds = 0
    for way in kept:
        coords = [(round(p["lon"], 5), round(p["lat"], 5)) for p in way["geometry"]]
        track_m += geo.line_m(coords)
        lines.append(coords)
        speed = _speed(way.get("tags") or {})
        speeds += speed is not None
        features.append({"id": f"w{way['id']}", "coords": [list(c) for c in coords],
                         "category": _speed_class(speed)})
    route_m = geo.route_m(lines)
    service_m = sum(geo.line_m([(p["lon"], p["lat"]) for p in w["geometry"]])
                    for w in service)

    category = None
    if railway and kept and speeds / len(kept) >= 0.5:
        # A legend heading is read, not parsed: the tags are in the footnote.
        category = {"field": "maxspeed", "label": f"{what}, by signalled maximum speed",
                    "ordered": True, "classes": list(SPEED_CLASSES)}
    else:
        for f in features:
            f["category"] = None

    route_km, track_km = route_m / 1000.0, track_m / 1000.0
    excluded = []
    if service:
        excluded.append({"count": len(service),
                         "reason": f"service tracks (sidings, yards, crossovers; "
                                   f"{service_m / 1000:,.0f} km)"})
    if nodes:
        excluded.append({"count": len(nodes), "reason": "points in a line layer"})
    coloured = "by signalled maximum speed" if category else ""
    return {
        "source": "overpass", "id": ";".join(f"{k}={v}" for k, v in pairs),
        "kind": "lines",
        "title": f"{what}{where}",
        "label": what,
        "size": None,
        "category": category,
        "features": features,
        "stats": {"count": len(features), "total_km": round(route_km),
                  "track_km": round(track_km)},
        "summary": (f"{what} in OpenStreetMap"
                    + (f", {coloured}" if coloured else "")
                    + f" · about {_about(route_km)} km of route"),
        "encoding": (("Colour = OSM maxspeed tag. " if category else "")
                     + f"{len(features):,} OSM ways = {track_km:,.0f} track-km; "
                     f"≈ {route_km:,.0f} km of route once parallel tracks within "
                     "20 m are merged"),
        "facts_label": "Network in OpenStreetMap",
        "facts": [("Route length", f"≈ {route_km:,.0f} km"),
                  ("Track length (all ways)", f"{track_km:,.0f} km"),
                  ("OSM ways", f"{len(features):,}")],
        "excluded": excluded,
        "provenance": provenance,
    }


def _about(km: float) -> str:
    step = 10 if km >= 1000 else 1
    return f"{round(km / step) * step:,.0f}"
