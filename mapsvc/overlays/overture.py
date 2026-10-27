"""Overture Maps as a stand-in for Overpass, for the tag families it answers.

Overture republishes OpenStreetMap (plus other sources) as GeoParquet on S3, and
DuckDB reads only the row groups a query touches, so a layer costs seconds to a
few minutes with no endpoint to be "too busy". It is *not* OSM: schema and
conflation differ, and only some of the OSM tag grammar has a faithful
counterpart. This module therefore answers a filter only when the mapping is
exact enough to state in a sentence, and otherwise returns None so the caller
keeps the Overpass error. It never guesses.

Supported (measured 2026-10, release 2026-10-21.0):

    railway=rail|narrow_gauge|subway|light_rail|tram|funicular|monorail
        transportation/segment, subtype=rail, class standard_gauge|unknown
        (rail), narrow_gauge, subway, light_rail, tram, funicular, monorail.
        Lifecycle flags is_abandoned / is_disused / is_under_construction are
        dropped (OSM's railway=rail excludes those states). Overture has no
        maxspeed, highspeed, electrified, usage or service attribute, so a
        filter with any other tag -- railway=rail;highspeed=yes included -- is
        NOT answered, and sidings and yards cannot be told from running line.
    place=city|town|village|hamlet
        divisions/division, subtype=locality, class city|town|village|hamlet.
        Localities with no class are left out and counted.

Not supported, and why: route relations (route=...; Overture has no relation
model); every other key (no counterpart); power=plant exists in
base/infrastructure but as mixed points and polygons that this point/line
renderer would not draw faithfully.

Licensed ODbL because OpenStreetMap is the primary source; the footnote credits
both OpenStreetMap and Overture and says which provider served the layer.
"""

import hashlib
import json
import os
import time

from mapsvc import fetch
from mapsvc.overlays import Window

RAIL_CLASSES = {
    "rail": ("standard_gauge", "unknown"),
    "narrow_gauge": ("narrow_gauge",),
    "subway": ("subway",),
    "light_rail": ("light_rail",),
    "tram": ("tram",),
    "funicular": ("funicular",),
    "monorail": ("monorail",),
}
# Overture class -> the OSM railway value it was derived from.
OSM_RAILWAY = {"standard_gauge": "rail", "unknown": "rail"}
SETTLEMENT_CLASSES = ("city", "town", "village", "hamlet")
LIFECYCLE_FLAGS = ("is_abandoned", "is_disused", "is_under_construction")
CACHE_FRESH_S = 7 * 24 * 60 * 60

SERVED_BY = "via Overture Maps (Overpass unavailable)"
ATTRIBUTION = ("© OpenStreetMap contributors, © Overture Maps Foundation, "
               + SERVED_BY)
LICENSE = "ODbL 1.0"

_S3 = "s3://overturemaps-us-west-2/release/{release}/theme={theme}/type={type}/*"


class Plan:
    """What a filter maps to: a family and its Overture class list."""

    def __init__(self, family: str, values: tuple, classes: tuple):
        self.family, self.values, self.classes = family, values, classes


def plan(spec) -> Plan | None:
    """The Overture query a validated filter maps to, or None if it cannot."""
    terms = spec.terms
    if len(terms) != 1 or terms[0][1] != "=" or spec.element == "relation":
        return None
    key, _, values = terms[0]
    if key == "railway" and all(v in RAIL_CLASSES for v in values):
        return Plan("rail", tuple(values),
                    tuple(c for v in values for c in RAIL_CLASSES[v]))
    if key == "place" and all(v in SETTLEMENT_CLASSES for v in values):
        return Plan("place", tuple(values), tuple(values))
    return None


def _source(release: str, theme: str, kind: str) -> str:
    return (f"read_parquet('{_S3.format(release=release, theme=theme, type=kind)}', "
            "hive_partitioning=1)")


def _quote(value: str) -> str:
    return value.replace("'", "''")


def _in(values) -> str:
    return ", ".join(f"'{_quote(v)}'" for v in values)


def _prefilter(window: Window) -> str:
    """Segment bounding box against the window's box (or the basemap extent)."""
    box = window.bbox or window.extent
    if box is None:
        return "true"
    w, s, e, n = box
    clause = f"bbox.ymin <= {n} AND bbox.ymax >= {s}"
    if e - w < 360:
        clause += f" AND bbox.xmin <= {e} AND bbox.xmax >= {w}"
    return clause


def _area_cte(window: Window) -> str:
    """The union of the window's `within` units, as one geometry named `area`."""
    from mapsvc.cartography import iso

    regions = [c.strip().upper() for c in window.within if "-" in c]
    countries = [iso.to_alpha2(c) for c in window.within if "-" not in c]
    if not all(countries):
        raise ValueError("unresolvable within code")
    parts = []
    if regions:
        parts.append(f"(subtype = 'region' AND region IN ({_in(regions)}))")
    if countries:
        parts.append(f"(subtype = 'country' AND country IN ({_in(countries)}))")
    return ("WITH area AS (SELECT ST_Union_Agg(geometry) AS g FROM "
            + _source("{release}", "divisions", "division_area")
            + " WHERE is_land AND (" + " OR ".join(parts) + "))")


def rail_sql(plan_: Plan, window: Window, release: str) -> str:
    """Pure: the segment query. Whole segments that touch the window, as
    Overpass returns whole ways."""
    flags = ", ".join(f"'{f}'" for f in LIFECYCLE_FLAGS)
    where = [
        "subtype = 'rail'", f"class IN ({_in(plan_.classes)})",
        _prefilter(window),
        "(rail_flags IS NULL OR NOT list_bool_or(list_transform(rail_flags, "
        f"f -> list_has_any(f.\"values\", [{flags}]))))",
    ]
    table = _source(release, "transportation", "segment")
    if window.within:
        cte = _area_cte(window).replace("{release}", release)
        where.append("ST_Intersects(geometry, area.g)")
        sql = f"{cte} SELECT id, class, ST_AsGeoJSON(geometry) FROM {table}, area "
    else:
        sql = f"SELECT id, class, ST_AsGeoJSON(geometry) FROM {table} "
    return sql + "WHERE " + " AND ".join(where)


def place_sql(plan_: Plan, window: Window, release: str) -> str:
    """Pure: the locality query. `region`/`country` are the unit's parent's
    codes, which is what a `within` of UA-07 or FRA means."""
    from mapsvc.cartography import overture

    where = ["subtype = 'locality'",
             f"(class IN ({_in(plan_.classes)}) OR class IS NULL)"]
    if window.within:
        where.append(overture._within(list(window.within)))
    if window.bbox is not None:
        w, s, e, n = window.bbox
        where.append(f"bbox.xmin >= {w} AND bbox.xmax <= {e} AND "
                     f"bbox.ymin >= {s} AND bbox.ymax <= {n}")
    elif not window.within and window.extent:
        w, s, e, n = window.extent
        where.append(f"bbox.xmin >= {w} AND bbox.xmax <= {e} AND "
                     f"bbox.ymin >= {s} AND bbox.ymax <= {n}")
    return ("SELECT id, class, names.common['en'], names.primary, population, "
            "ST_X(geometry), ST_Y(geometry) FROM "
            + _source(release, "divisions", "division") + " WHERE "
            + " AND ".join(where))


def _rows(sql: str) -> list:
    """Query rows, cached on disk by the exact SQL (which names the release)."""
    digest = hashlib.sha256(sql.encode()).hexdigest()[:16]
    target = fetch.path("overlays", "overture", f"{digest}.json")
    if target.exists() and time.time() - target.stat().st_mtime <= CACHE_FRESH_S:
        return json.loads(target.read_text())
    from mapsvc.cartography import overture

    rows = overture._connection().execute(sql).fetchall()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".part")
    temporary.write_text(json.dumps(rows))
    temporary.replace(target)
    return rows


def elements_from_rail(rows: list) -> list:
    """Overpass-shaped ways from segment rows, in a deterministic order."""
    elements = []
    for oid, cls, geometry in sorted(rows, key=lambda r: r[0]):
        gj = json.loads(geometry)
        parts = ([gj["coordinates"]] if gj["type"] == "LineString"
                 else gj["coordinates"])
        for part in parts:
            if len(part) < 2:
                continue
            elements.append({
                "type": "way", "id": len(elements) + 1,
                "tags": {"railway": OSM_RAILWAY.get(cls, cls)},
                "geometry": [{"lon": p[0], "lat": p[1]} for p in part]})
    return elements


def elements_from_places(rows: list) -> tuple[list, int]:
    """Overpass-shaped place nodes, and how many classless localities."""
    nodes, classless = [], 0
    for oid, cls, name_en, name, population, lon, lat in sorted(rows, key=lambda r: r[0]):
        if not cls:
            classless += 1
            continue
        tags = {"place": cls}
        if name_en or name:
            tags["name"] = name_en or name
        if population:
            tags["population"] = str(population)
        nodes.append({"type": "node", "id": len(nodes) + 1, "lat": lat, "lon": lon,
                      "tags": tags})
    return nodes, classless


def load(spec, window: Window) -> dict | None:
    """A layer for `spec`, or None when Overture cannot answer it faithfully
    (unmappable filter, or Overture itself failing). Never raises for those."""
    from mapsvc.cartography import overture
    from mapsvc.overlays import overpass

    if os.environ.get("MAPSVC_OVERTURE_FALLBACK") == "0":
        return None
    chosen = plan(spec)
    if chosen is None:
        return None
    try:
        release = overture.release()
        started = time.monotonic()
        if chosen.family == "rail":
            sql = rail_sql(chosen, window, release)
            data = {"elements": elements_from_rail(_rows(sql))}
            classless = 0
        else:
            sql = place_sql(chosen, window, release)
            nodes, classless = elements_from_places(_rows(sql))
            data = {"elements": nodes}
        if not data["elements"]:
            return None
        layer = overpass.layer_from_overpass(data, spec, window, sql)
    except Exception:       # noqa: BLE001 -- keep Overpass's error, not ours
        return None
    return _restate(layer, spec, window, release, chosen, classless,
                    round(time.monotonic() - started, 1))


def _restate(layer: dict, spec, window: Window, release: str, chosen: Plan,
             classless: int, seconds: float) -> dict:
    """Make the layer say what it really is: Overture, not Overpass."""
    where = f" in {window.name}" if window.name else " in the map window"
    caveats = {
        "rail": "route relations and speed, electrification and service tags "
                "are not available from Overture, so running line, sidings and "
                "yards are not separated",
        "place": "settlements are Overture localities classed city, town, village "
                 "or hamlet",
    }[chosen.family]
    layer["provenance"] = {
        "name": "OpenStreetMap via Overture Maps",
        "attribution": ATTRIBUTION,
        "license": LICENSE,
        "url": None,
        "vintage": (f"Overture Maps release {release}, {SERVED_BY}: every "
                    + ("railway segment" if chosen.family == "rail" else "locality")
                    + f" of class {'|'.join(chosen.values)}{where}; {caveats}"),
    }
    for field in ("summary", "encoding"):
        if layer.get(field):
            layer[field] = (layer[field].replace("in OpenStreetMap", "in Overture Maps")
                            .replace("OSM ways", "Overture segments")
                            .replace("OSM place node", "Overture locality"))
    if classless:
        layer.setdefault("excluded", []).append(
            {"count": classless, "reason": "localities with no city/town/village/"
                                           "hamlet class in Overture"})
    layer["served_by"] = "overture"
    layer["fetch_seconds"] = seconds
    return layer
