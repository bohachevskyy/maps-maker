"""OpenStreetMap features via the Overpass API (ODbL).

The id is a tag filter in a deliberately small grammar. The service writes
the Overpass QL; the model never does, so it cannot ask for the planet or run
arbitrary queries on a public server.

    railway=rail;highspeed=yes                 ways (and nodes), tags ANDed
    place=city|town|village|hamlet             a list of values: any of them
    route=subway;network=東京メトロ|都営地下鉄     route relations, one line each
    route=railway;name~Trans-Siberian          a name, matched as text

Vocabulary tags are concrete key=value pairs in lower case [a-z0-9_:]; at
least one is required, and each value is checked against taginfo. Names
(name, network, operator, ref, brand) may be any text of letters, digits,
spaces, hyphens and apostrophes -- nothing that could close a QL string or
mean something to a regex -- and `~` matches one as a case-insensitive
substring of `name` or any `name:xx`.

Which OSM elements to read is derived from the tags, not written: `route=` is
a relation tag (one feature per line, its ways merged, its stops as stations),
a settlement `place=` is a node tag, anything else is ways and nodes. A
`relation:`, `node:` or `way:` prefix is accepted, and refused when it
contradicts the tags.

The area is the manifest's: `within` becomes Overpass areas by ISO 3166 code
(the country itself, so a filter inside France does not wander into the
Channel Tunnel's English half; UA-07 is Volyn oblast's boundary, not its box),
a bbox becomes a box filter, both narrow twice.

Measured 2026-10-07 on overpass-api.de: France, railway=rail + highspeed=yes,
area query 145 s and 11 MB; the same as a bbox 108 s and 17 MB (neighbours
included). The server-side timeout is 400 s because 180 s was exceeded at 01:35 local
that night. Queries this slow are the reason every response is cached on disk,
keyed by the exact QL, and why other public endpoints are tried when the
first refuses (504 "too busy" was common that night). overpass-api.de answers
406 to a request without a User-Agent.
"""

import dataclasses
import hashlib
import json
import math
import os
import re
import time
import urllib.parse

from mapsvc import fetch
from mapsvc.overlays import Capabilities, OverlayError, Window, geo

# Measured 2026-10-07 21:00 local, a dozen queries: overpass-api.de answered
# 504 to about half, private.coffee and kumi.systems 500 to all; maps.mail.ru
# answered every query it was offered, 22-32 s.
ENDPOINTS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
)
TIMEOUT_S = 400
DEFAULT_BUDGET_S = 300
STATUS_TIMEOUT_S = 2
MAXSIZE = 512 * 1024 * 1024
MAX_WAYS = 40_000
MAX_RELATIONS = 400
MAX_PAIRS = 4
MAX_VALUES = 8
TAGINFO_TIMEOUT_S = 3
TAGINFO_MIN_COUNT = 100
TAGINFO_VALUES = 5
TAGINFO = "https://taginfo.openstreetmap.org/api/4"

# These are commonly used as yes/no qualifiers. A wildcard is still rejected:
# for these keys it is especially likely that the request meant "=yes".
BOOLEAN_KEYS = {
    "bridge", "covered", "highspeed", "lit", "oneway", "tunnel",
    "wheelchair",
}
# Keys whose values are names rather than OSM vocabulary: any script, not
# checked against taginfo (a network's name is rare by nature), and never
# enough on their own to anchor a query.
TEXT_KEYS = {"name", "network", "operator", "ref", "brand", "official_name",
             "int_name"}
# Settlement places live on nodes; an area tagged place=village is the same
# village drawn twice.
SETTLEMENTS = ("city", "town", "village", "hamlet")
PLACE_NODES = set(SETTLEMENTS) | {"suburb", "quarter", "neighbourhood",
                                  "isolated_dwelling", "locality"}
ELEMENTS = ("node", "way", "relation")

CAPABILITIES = Capabilities(
    source="overpass",
    license="ODbL 1.0",
    attribution="© OpenStreetMap contributors",
    description="any OpenStreetMap feature selected by tags -- railways, "
                "motorways, rivers, power lines, stations, settlements -- and "
                "named transit or railway lines as OSM route relations, inside "
                "the map's within/bbox, drawn as lines or points. Slow when "
                "uncached (one to three minutes for a country), then instant",
    id_help="semicolon-separated tags, ANDed. Vocabulary tags are concrete "
            "lower-case key=value pairs [a-z0-9_:], at least one, e.g. "
            "'railway=rail;highspeed=yes' or 'power=line'; 'key=a|b|c' means "
            "any of those values, e.g. 'place=city|town|village|hamlet'. A route "
            "tag selects OSM route relations, one feature per line with its "
            "colour: 'route=subway;network=<network name as tagged in OSM>' or "
            "'route=railway;name~Trans-Siberian'. Names (name, network, "
            "operator, ref) may be any text; name~text matches part of a name "
            "in any language",
)

_TOKEN = re.compile(r"[a-z0-9_:]+")
_VALUE = re.compile(r"[a-z0-9_:.\-]+|\*")
# Letters and digits of any script, spaces, hyphens, apostrophes: no quote or
# backslash to escape a QL string, no character with a meaning in a regex.
_TEXT = re.compile(r"[^\W_][\w '’\-]{0,59}")


@dataclasses.dataclass(frozen=True)
class Filter:
    """A parsed id: which elements, and terms (key, "=" or "~", values)."""

    element: str          # "relation", "node", "way", or "" for ways and nodes
    terms: tuple

    def vocabulary(self) -> list[tuple[str, str]]:
        """Every concrete key=value the filter names, one pair per value."""
        return [(k, v) for k, op, values in self.terms
                if op == "=" and not _is_text(k) for v in values]

    def pairs(self) -> list[tuple[str, str]]:
        """Terms as (key, value) for descriptions; alternatives joined by |."""
        return [(k, "|".join(values)) if op == "=" else (k, "~" + values[0])
                for k, op, values in self.terms]

    def text(self) -> str:
        body = ";".join(f"{k}{op}{'|'.join(values)}" for k, op, values in self.terms)
        return f"{self.element}:{body}" if self.element else body

    def values(self, key: str) -> tuple:
        return next((values for k, op, values in self.terms
                     if k == key and op == "="), ())


def _is_text(key: str) -> bool:
    return key in TEXT_KEYS or key.startswith("name:")


def parse(overlay_id: str) -> str:
    return parse_filter(overlay_id).text()


def _pairs(overlay_id: str) -> list[tuple[str, str]]:
    return parse_filter(overlay_id).pairs()


def parse_filter(overlay_id: str) -> Filter:
    raw = overlay_id.strip()
    prefix = ""
    head = re.match(r"(node|way|relation):(?=[a-z])", raw)
    if head and "=" not in raw[:head.end()]:
        prefix, raw = head.group(1), raw[head.end():]
    terms = []
    for part in raw.split(";"):
        part = part.strip()
        if not part:
            continue
        terms.append(_term(part))
    keys = [k for k, _, _ in terms]
    for key in keys:
        if keys.count(key) > 1:
            raise OverlayError(f"overpass filter repeats {key!r}", "overlays.id")
    if not terms:
        raise OverlayError(f"overpass filter is empty; {CAPABILITIES.id_help}",
                           "overlays.id")
    if len(terms) > MAX_PAIRS:
        raise OverlayError(f"overpass filter has {len(terms)} pairs, at most "
                           f"{MAX_PAIRS}", "overlays.id")
    if all(_is_text(k) for k in keys):
        raise OverlayError(
            "an overpass filter needs at least one vocabulary tag saying what kind "
            "of feature it is (route=train, railway=rail, place=town); names only "
            "narrow it", "overlays.id")
    derived = _element_for(terms)
    if prefix and derived and prefix != derived:
        raise OverlayError(
            f"{'route' if derived == 'relation' else 'place'} tags are on OSM "
            f"{derived}s, not {prefix}s; drop the '{prefix}:' prefix",
            "overlays.id")
    return Filter(prefix or derived, tuple(terms))


def _term(part: str) -> tuple:
    tilde, equals = part.find("~"), part.find("=")
    if tilde > 0 and (equals < 0 or tilde < equals):
        key, value = (s.strip() for s in part.split("~", 1))
        if not _is_text(key):
            raise OverlayError(
                f"overpass filter {part!r}: only names (name, network, operator, "
                "ref) can be matched with ~; vocabulary tags need key=value",
                "overlays.id")
        if not _TOKEN.fullmatch(key) or not _TEXT.fullmatch(value) or len(value) < 3:
            raise OverlayError(
                f"overpass name match {part!r} must be 3-60 letters, digits, "
                "spaces, hyphens or apostrophes", "overlays.id")
        return (key, "~", (value,))
    if part.count("=") != 1:
        raise OverlayError(f"overpass filter part {part!r} is not key=value; "
                           f"{CAPABILITIES.id_help}", "overlays.id")
    key, value = (s.strip() for s in part.split("="))
    values = tuple(v.strip() for v in value.split("|"))
    if not _TOKEN.fullmatch(key):
        raise OverlayError(
            f"overpass filter {part!r} uses characters outside [a-z0-9_:]; "
            "free Overpass QL is not accepted", "overlays.id")
    if len(values) > MAX_VALUES or len(set(values)) != len(values):
        raise OverlayError(f"overpass filter {part!r}: at most {MAX_VALUES} "
                           "distinct values", "overlays.id")
    if _is_text(key):
        bad = [v for v in values if not _TEXT.fullmatch(v)]
        if bad:
            raise OverlayError(
                f"overpass name {bad[0]!r} must be letters, digits, spaces, "
                "hyphens or apostrophes", "overlays.id")
        return (key, "=", values)
    for v in values:
        if not _VALUE.fullmatch(v):
            raise OverlayError(
                f"overpass filter {part!r} uses characters outside [a-z0-9_:]; "
                "free Overpass QL is not accepted", "overlays.id")
        if v == "*":
            hint = (f"; use {key}=yes if that is what you mean"
                    if key in BOOLEAN_KEYS else "; choose a concrete value")
            raise OverlayError(
                f"overpass wildcard {key}=* is too broad{hint}", "overlays.id")
    return (key, "=", values)


def _element_for(terms) -> str:
    for key, op, values in terms:
        if key == "route" and op == "=":
            return "relation"
    for key, op, values in terms:
        if key == "place" and op == "=" and set(values) <= PLACE_NODES:
            return "node"
    return ""


def _as_filter(spec) -> Filter:
    if isinstance(spec, Filter):
        return spec
    if isinstance(spec, str):
        return parse_filter(spec)
    return Filter("", tuple((k, "=", tuple(v.split("|"))) for k, v in spec))


def _taginfo_json(path: str, params: dict) -> dict:
    query_string = urllib.parse.urlencode(params)
    url = f"{TAGINFO}/{path}?{query_string}"
    digest = hashlib.sha256(url.encode()).hexdigest()[:16]
    payload = fetch.cached(url, "overlays", "taginfo", f"{digest}.json",
                           timeout=TAGINFO_TIMEOUT_S, accept="application/json")
    return json.loads(payload)


def _suggestions(key: str) -> list[str]:
    data = _taginfo_json("key/values", {
        "key": key, "page": 1, "rp": TAGINFO_VALUES,
        "sortname": "count_all", "sortorder": "desc",
    })
    return [str(item["value"]) for item in data.get("data", [])
            if item.get("value")]


def validate_tags(pairs) -> None:
    """Reject invented or vanishingly rare tags before spending Overpass time.

    Taginfo is advisory infrastructure: if it is unavailable or returns an
    unusable response, Overpass remains the authority and the query proceeds.
    Successful responses are cached on disk by exact URL.
    """
    for key, value in pairs:
        try:
            data = _taginfo_json("tag/stats", {"key": key, "value": value})
            all_rows = [row for row in data.get("data", [])
                        if row.get("type") == "all"]
            if not all_rows:
                continue
            count = int(all_rows[0].get("count", 0))
        except (fetch.FetchError, json.JSONDecodeError, TypeError, ValueError, KeyError):
            continue
        if count >= TAGINFO_MIN_COUNT:
            continue
        try:
            common = _suggestions(key)
        except (fetch.FetchError, json.JSONDecodeError, TypeError, ValueError, KeyError):
            common = []
        suggestion = (f" Common {key} values: "
                      + ", ".join(f"{key}={item}" for item in common) + "."
                      if common else "")
        raise OverlayError(
            f"OSM tag {key}={value} has only {count:,} uses worldwide; "
            f"check the tag.{suggestion}", "overlays.id")


def query(spec, window: Window) -> str:
    """The Overpass QL for a filter and a window. Pure, so it is testable."""
    f = _as_filter(spec)
    tags = "".join(_ql_term(term) for term in f.terms)
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
        if any("-" in code for code in window.within):
            # A subdivision's own name, for the title: "Volyn Oblast".
            head += ".a out tags;"
        scope += "(area.a)"
    if window.bbox is not None:
        w, s, e, n = window.bbox
        scope += f"({s:g},{w:g},{n:g},{e:g})"
    if not scope:
        raise OverlayError("an overpass overlay needs a within or a bbox", "overlays")
    if f.element == "relation":
        # Members with geometry; the member nodes again for their names (the
        # stops); and the route_masters that group a line's variants.
        return (head + f"relation{tags}{scope}->.r;.r out geom;"
                "node(r.r)->.s;.s out tags geom;"
                'rel(br.r)["type"="route_master"];out body;')
    if f.element in ("node", "way"):
        return head + f"{f.element}{tags}{scope};out tags geom;"
    return head + f"(way{tags}{scope};node{tags}{scope};);out tags geom;"


def _ql_term(term) -> str:
    key, op, values = term
    if op == "~":
        # Validated text: no quotes, backslashes or regex metacharacters.
        if key == "name":
            return f'[~"^name(:[a-z_]+)?$"~"{values[0]}",i]'
        return f'["{key}"~"{values[0]}",i]'
    if len(values) == 1:
        return f'["{key}"]' if values[0] == "*" else f'["{key}"="{values[0]}"]'
    alternatives = "|".join(v.replace(".", "[.]") for v in values)
    return f'["{key}"~"^({alternatives})$"]'


def _alpha2(code: str) -> str:
    from mapsvc.cartography import iso

    two = iso.to_alpha2(code)
    if not two or not re.fullmatch(r"[A-Z]{2}", two):
        raise OverlayError(f"no ISO 3166-1 code for {code!r}; Overpass areas are "
                           "found by ISO code", "within")
    return two


def load(overlay_id: str, window: Window) -> dict:
    f = parse_filter(overlay_id)
    validate_tags(f.vocabulary())
    ql = query(f, window)
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
    if window.name is None and (name := _area_name(data)):
        window = dataclasses.replace(window, name=name)
    if f.element == "relation":
        layer = layer_from_relations(data, f, window)
    else:
        layer = layer_from_overpass(data, f, window, ql)
    if elapsed is not None:
        layer["fetch_seconds"] = round(elapsed, 1)
    return layer


def _area_name(data: dict) -> str | None:
    names = sorted({(e.get("tags") or {}).get("name:en") or (e.get("tags") or {}).get("name")
                    for e in data.get("elements") or () if e.get("type") == "area"} - {None})
    return " and ".join(names) or None


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
    ("route", "subway"): "metro lines", ("route", "train"): "train routes",
    ("route", "railway"): "railway lines", ("route", "tram"): "tram routes",
    ("route", "light_rail"): "light-rail lines", ("route", "bus"): "bus routes",
    ("route", "trolleybus"): "trolleybus routes", ("route", "ferry"): "ferry routes",
    ("route", "monorail"): "monorail lines",
}
_MODIFIERS = {("highspeed", "yes"): "high-speed", ("electrified", "contact_line"): "electrified",
              ("usage", "main"): "main"}
_PLACE_NOUNS = {"city": "cities", "town": "towns", "village": "villages",
                "hamlet": "hamlets", "suburb": "suburbs", "quarter": "quarters",
                "neighbourhood": "neighbourhoods", "locality": "localities",
                "isolated_dwelling": "isolated dwellings"}


def describe(pairs) -> str:
    """A plain phrase for a filter: 'High-speed railway lines', 'Towns and
    villages'. Names narrowing a filter are left to the footnote."""
    pairs = [(k, v) for k, v in (_as_filter(pairs).pairs() if isinstance(pairs, Filter)
                                 else pairs) if not _is_text(k)]
    places = [v for k, v in pairs if k == "place"]
    if places and len(pairs) == 1:
        values = places[0].split("|")
        if set(values) <= set(_PLACE_NOUNS):
            words = [_PLACE_NOUNS[v] for v in values]
            text = (", ".join(words[:-1]) + " and " + words[-1]) if len(words) > 1 else words[0]
            return text[:1].upper() + text[1:]
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
    spec = _as_filter(pairs)
    pairs = spec.pairs()
    elements = data.get("elements") or []
    ways = [e for e in elements if e.get("type") == "way" and len(e.get("geometry") or []) >= 2]
    nodes = [e for e in elements if e.get("type") == "node" and "lat" in e]
    if not ways and not nodes:
        raise OverlayError(
            "OpenStreetMap filter matched 0 features; check the tag or widen "
            "the window", "overlays.id")
    vintage = (data.get("osm3s") or {}).get("timestamp_osm_base")
    what = describe(pairs)
    where = f" in {window.name}" if window.name else ""
    provenance = _provenance(vintage, spec, window)

    if not ways:
        if len(nodes) > MAX_WAYS:
            raise OverlayError(f"{len(nodes):,} OpenStreetMap points match; narrow "
                               "the filter or the window", "overlays.id")
        if spec.values("place") and set(spec.values("place")) <= PLACE_NODES:
            return _settlements(nodes, spec, what, where, provenance)
        features = [{"id": f"n{n['id']}", "lon": round(n["lon"], 5), "lat": round(n["lat"], 5),
                     "size": None, "category": None,
                     "name": (n.get("tags") or {}).get("name")} for n in nodes]
        features.sort(key=lambda f: f["id"])
        return {
            "source": "overpass", "id": spec.text(),
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
        "source": "overpass", "id": spec.text(),
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


def _provenance(vintage: str | None, spec: Filter, window: Window) -> dict:
    return {
        "name": "OpenStreetMap",
        "attribution": CAPABILITIES.attribution + ", via the Overpass API",
        "license": CAPABILITIES.license,
        "url": None,
        "vintage": ((f"data as of {vintage[:10]}: " if vintage else "")
                    + ("every route relation tagged " if spec.element == "relation"
                       else "every element tagged ")
                    + " + ".join(f"{k}{op}{'|'.join(v)}" for k, op, v in spec.terms)
                    + (f" in {window.name}" if window.name else " in the map window")),
    }


def _name(tags: dict) -> str | None:
    """The English name where OSM has one: the rest of the map is English."""
    return tags.get("name:en") or tags.get("name") or None


# ------------------------------------------------------------ settlements ---

_PLACE_LABELS = {"city": "City", "town": "Town", "village": "Village",
                 "hamlet": "Hamlet", "suburb": "Suburb", "quarter": "Quarter",
                 "neighbourhood": "Neighbourhood", "locality": "Locality",
                 "isolated_dwelling": "Isolated dwelling"}
_PLACE_ORDER = ("city", "town", "suburb", "village", "quarter", "neighbourhood",
                "hamlet", "isolated_dwelling", "locality")


def _population(tags: dict) -> int | None:
    match = re.fullmatch(r"\s*(\d[\d ,]*)\s*", str(tags.get("population") or ""))
    return int(re.sub(r"[ ,]", "", match.group(1))) if match else None


def _settlements(nodes: list, spec: Filter, what: str, where: str,
                 provenance: dict) -> dict:
    """Place nodes as a hierarchy: the place tag decides symbol and label.

    OSM's place=city/town/village/hamlet is the settlement's own rank, so the
    symbology is derived from it, as a choropleth's ramp is from its values.
    """
    features = []
    for n in nodes:
        tags = n.get("tags") or {}
        features.append({"id": f"n{n['id']}", "lon": round(n["lon"], 5),
                         "lat": round(n["lat"], 5), "size": None,
                         "category": tags.get("place"), "name": _name(tags),
                         "population": _population(tags)})
    features.sort(key=lambda f: f["id"])
    counts = {}
    for f in features:
        counts[f["category"]] = counts.get(f["category"], 0) + 1
    classes = [{"value": v, "label": f"{_PLACE_LABELS[v]} ({counts[v]:,})"}
               for v in _PLACE_ORDER if counts.get(v)]
    stats = {"count": len(features)}
    stats.update({f"count_{v}": counts[v] for v in _PLACE_ORDER if counts.get(v)})
    breakdown = ", ".join(f"{counts[v]:,} {_PLACE_NOUNS[v] if counts[v] != 1 else v}"
                          for v in _PLACE_ORDER if counts.get(v))
    return {
        "source": "overpass", "id": spec.text(), "kind": "points",
        "style": "settlements",
        # The layer names its own places; Natural Earth labels would repeat them.
        "own_labels": True,
        "title": f"{what}{where}", "label": what, "size": None,
        "category": {"field": "place", "label": "Settlement (OSM place tag)",
                     "ordered": True, "classes": classes},
        "features": features, "stats": stats,
        "summary": f"{len(features):,} settlements in OpenStreetMap: {breakdown}",
        "encoding": ("One symbol per OSM place node, sized by its place tag; "
                     "cities and towns always named, villages where room allows"),
        "excluded": [], "provenance": provenance,
    }


# ----------------------------------------------------------------- routes ---

# OSM's colour tag is free text: a hex code or a CSS colour name, sometimes
# neither. Only what validates is used; the rest get a palette colour.
CSS_COLOURS = {
    "black": "#000000", "white": "#ffffff", "red": "#ff0000", "green": "#008000",
    "blue": "#0000ff", "yellow": "#ffff00", "orange": "#ffa500", "purple": "#800080",
    "pink": "#ffc0cb", "brown": "#a52a2a", "gray": "#808080", "grey": "#808080",
    "cyan": "#00ffff", "aqua": "#00ffff", "magenta": "#ff00ff", "fuchsia": "#ff00ff",
    "lime": "#00ff00", "navy": "#000080", "maroon": "#800000", "olive": "#808000",
    "teal": "#008080", "silver": "#c0c0c0", "gold": "#ffd700", "violet": "#ee82ee",
    "indigo": "#4b0082", "darkgreen": "#006400", "darkblue": "#00008b",
    "darkred": "#8b0000", "lightblue": "#add8e6", "lightgreen": "#90ee90",
    "skyblue": "#87ceeb", "turquoise": "#40e0d0", "crimson": "#dc143c",
    "coral": "#ff7f50", "salmon": "#fa8072", "khaki": "#f0e68c", "beige": "#f5f5dc",
    "tan": "#d2b48c", "chocolate": "#d2691e", "orchid": "#da70d6", "plum": "#dda0dd",
    "lavender": "#e6e6fa", "darkorange": "#ff8c00", "deepskyblue": "#00bfff",
    "dodgerblue": "#1e90ff", "royalblue": "#4169e1", "steelblue": "#4682b4",
    "seagreen": "#2e8b57", "forestgreen": "#228b22", "limegreen": "#32cd32",
    "firebrick": "#b22222", "tomato": "#ff6347", "hotpink": "#ff69b4",
    "deeppink": "#ff1493", "darkviolet": "#9400d3", "slategray": "#708090",
    "slategrey": "#708090", "lightgray": "#d3d3d3", "lightgrey": "#d3d3d3",
    "darkgray": "#a9a9a9", "darkgrey": "#a9a9a9", "midnightblue": "#191970",
    "mediumblue": "#0000cd", "darkcyan": "#008b8b", "yellowgreen": "#9acd32",
    "olivedrab": "#6b8e23", "sienna": "#a0522d", "saddlebrown": "#8b4513",
    "goldenrod": "#daa520", "darkgoldenrod": "#b8860b", "mediumpurple": "#9370db",
    "rebeccapurple": "#663399", "cornflowerblue": "#6495ed", "cadetblue": "#5f9ea0",
}
_HEX = re.compile(r"#?([0-9a-f]{3}|[0-9a-f]{6})")


def osm_colour(raw) -> str | None:
    """A validated #rrggbb for an OSM colour tag, or None."""
    text = str(raw or "").strip().lower()
    if text in CSS_COLOURS:
        return CSS_COLOURS[text]
    match = _HEX.fullmatch(text)
    if not match:
        return None
    digits = match.group(1)
    if len(digits) == 3:
        digits = "".join(c * 2 for c in digits)
    return "#" + digits


def line_name(tags: dict, local: bool = False) -> str:
    """A line's name without its direction: 'Ginza Line (Asakusa --> Shibuya)'
    and 'Tōzai Line Local: Nakano => Nishi-Funabashi' name the line, not the
    variant. `local` reads `name` rather than the English name, for grouping:
    not every variant has a name:en."""
    text = (tags.get("name") if local else _name(tags)) or tags.get("ref") or ""
    text = re.sub(r"\s*[(（][^()（）]*[)）]", "", text)
    text = re.sub(r"\s*:\s*[^:]*(->|=>|→|-->|⇒).*$", "", text)
    text = re.sub(r"\s*[^\s]+\s*(->|=>|→|-->|⇒)\s*[^\s]+.*$", "", text)
    return text.strip(" -:") or (tags.get("ref") or "")


STATION_KM = 1.2
_SKIP_ROLES = ("platform", "stop")


def layer_from_relations(data: dict, spec, window: Window) -> dict:
    """Route relations as lines: one per line, coloured by its own tags.

    OSM keeps each direction and service pattern of a line as its own route
    relation (82 route=subway relations around Tokyo for 13 Metro and Toei
    lines); a route_master groups them. Relations under one master are one
    line; relations without one are grouped by their name with the direction
    removed ("Trans-Siberian Railway (even direction)" and "(odd direction)"
    are one railway). A route=subway relation whose master is a route_master
    =train is a through service onto another company's tracks, not a line of
    the network, and is excluded.
    """
    spec = _as_filter(spec)
    elements = data.get("elements") or []
    routes = sorted((e for e in elements if e.get("type") == "relation"
                     and (e.get("tags") or {}).get("type") != "route_master"
                     and e.get("members")), key=lambda e: e["id"])
    masters = sorted((e for e in elements if e.get("type") == "relation"
                      and (e.get("tags") or {}).get("type") == "route_master"),
                     key=lambda e: e["id"])
    nodes = {e["id"]: e for e in elements if e.get("type") == "node"}
    if not routes:
        hint = ""
        if spec.values("route"):
            hint = ("; route=railway is the physical line, route=train a "
                    "passenger service over it, and a name~ match is against "
                    "how OSM spells it")
        raise OverlayError("OpenStreetMap filter matched 0 route relations; "
                           f"check the tags or widen the window{hint}", "overlays.id")
    if len(routes) > MAX_RELATIONS:
        raise OverlayError(f"{len(routes):,} OpenStreetMap route relations match; "
                           "narrow the filter (a network, a name) or the window",
                           "overlays.id")
    master_of = {}
    for m in masters:
        for member in m.get("members") or ():
            if member.get("type") == "relation":
                master_of.setdefault(member["ref"], m)

    groups: dict = {}
    through = 0
    for r in routes:
        tags = r.get("tags") or {}
        master = master_of.get(r["id"])
        kind = (master or {}).get("tags", {}).get("route_master")
        if master and tags.get("route") and kind and kind != tags["route"]:
            through += 1
            continue
        if master:
            key, identity = f"m{master['id']}", master.get("tags") or {}
        else:
            key, identity = "n" + line_name(tags, local=True), tags
        group = groups.setdefault(key, {"identity": identity, "relations": []})
        group["relations"].append(r)

    lines_out, features, classes = [], [], []
    track_m, every_way = 0.0, []
    stops: list = []
    for key in sorted(groups):
        group = groups[key]
        ways: dict = {}
        for r in group["relations"]:
            for member in r.get("members") or ():
                role = member.get("role") or ""
                if member.get("type") == "way" and not role.startswith(_SKIP_ROLES):
                    geometry = member.get("geometry") or []
                    if len(geometry) >= 2 and member["ref"] not in ways:
                        ways[member["ref"]] = [(round(p["lon"], 5), round(p["lat"], 5))
                                               for p in geometry]
                elif member.get("type") == "node" and role.startswith("stop"):
                    stops.append((member["ref"], key, member.get("lon"), member.get("lat")))
        if not ways:
            continue
        raw = [ways[w] for w in sorted(ways)]
        track_m += sum(geo.line_m(c) for c in raw)
        every_way.extend(raw)
        chained = geo.chain(raw)
        identity = group["identity"]
        colour = osm_colour(identity.get("colour"))
        for r in group["relations"]:
            colour = colour or osm_colour((r.get("tags") or {}).get("colour"))
        first = group["relations"][0].get("tags") or {}
        name = line_name(identity)
        classes.append({"value": key, "label": name or key, "colour": colour,
                        "ref": identity.get("ref") or first.get("ref"),
                        "network": identity.get("network") or first.get("network"),
                        # Measured on the ways: a chain can run out on one
                        # track and back on the other, and route_m merges
                        # only *different* lines.
                        "km": round(geo.route_m(raw) / 1000.0, 1),
                        "relations": len(group["relations"])})
        for i, coords in enumerate(chained):
            features.append({"id": f"{key}#{i}", "coords": [list(c) for c in coords],
                             "category": key})
    if not classes:
        raise OverlayError("the matching route relations have no track geometry",
                           "overlays.id")
    classes.sort(key=lambda c: (str(c["network"] or ""), _ref_key(c["ref"]), c["label"]))
    route_km = geo.route_m(every_way) / 1000.0
    track_km = track_m / 1000.0
    stations = _stations(stops, nodes, {c["value"] for c in classes})

    what = describe(spec)
    where = f" in {window.name}" if window.name else ""
    single = len(classes) == 1
    title = (classes[0]["label"] if single else what) + where
    vintage = (data.get("osm3s") or {}).get("timestamp_osm_base")
    excluded = []
    if through:
        excluded.append({"count": through,
                         "reason": "through-service relations (another network's "
                                   "trains running over these lines)"})
    facts = [("Route length", f"≈ {route_km:,.0f} km")]
    if not single:
        facts.append(("Lines", f"{len(classes):,}"))
    if stations:
        facts.append(("Stations", f"{len(stations):,}"))
    facts.append(("OSM route relations", f"{sum(c['relations'] for c in classes):,}"))
    stats = {"count": len(classes), "total_km": round(route_km),
             "track_km": round(track_km)}
    if stations:
        stats["stations"] = len(stations)
    coloured = sum(1 for c in classes if c["colour"])
    return {
        "source": "overpass", "id": spec.text(), "kind": "lines",
        "style": "routes",
        "title": title, "label": what, "size": None,
        "category": {"field": "route", "label": "" if single else what,
                     "classes": classes},
        "features": features,
        "stations": stations,
        "stats": stats,
        "drawn": len(classes), "noun": "lines" if not single else "line",
        "summary": ((f"{len(classes):,} lines, " if not single else "")
                    + f"about {_about(route_km)} km of route"),
        "encoding": ((f"Colour = each line's OSM colour tag ({coloured} of "
                      f"{len(classes)} tagged). " if not single else "")
                     + f"{sum(c['relations'] for c in classes):,} OSM route relations "
                     f"merged into {len(classes):,} line{'s' if not single else ''}; "
                     f"≈ {route_km:,.0f} km of route once parallel tracks within "
                     f"20 m are merged ({track_km:,.0f} km of track ways)"),
        "facts_label": "In OpenStreetMap",
        "facts": facts,
        "excluded": excluded,
        "provenance": _provenance(vintage, spec, window),
    }


def _ref_key(ref) -> tuple:
    text = str(ref or "")
    number = re.match(r"\d+", text)
    return (int(number.group()) if number else 1 << 30, text)


def _stations(stops: list, nodes: dict, lines: set) -> list:
    """Stops grouped into stations by name and distance, with the lines serving
    each. A line's stop positions in both directions, and two lines' stops at
    one interchange, are one station: same name, within STATION_KM."""
    clusters: list = []           # [local name, lon, lat, label, set(lines)]
    for ref, key, lon, lat in sorted(stops, key=lambda s: (s[0], s[1])):
        if key not in lines:
            continue
        node = nodes.get(ref) or {}
        tags = node.get("tags") or {}
        local = tags.get("name")
        lon = node.get("lon", lon)
        lat = node.get("lat", lat)
        if not local or lon is None or lat is None:
            continue
        for c in clusters:
            if c[0] == local and geo.segment_m((c[1], c[2]), (lon, lat)) <= STATION_KM * 1000:
                c[4].add(key)
                if not c[3] and tags.get("name:en"):
                    c[3] = tags["name:en"]
                break
        else:
            clusters.append([local, lon, lat, tags.get("name:en"), {key}])
    out = [{"name": c[3] or c[0], "lon": round(c[1], 5), "lat": round(c[2], 5),
            "lines": len(c[4])} for c in clusters]
    out.sort(key=lambda s: (-s["lines"], s["name"], s["lon"], s["lat"]))
    return out
