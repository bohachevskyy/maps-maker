"""Overture Maps divisions, queried straight off S3 with DuckDB.

No bulk download: the data is Hive-partitioned GeoParquet and DuckDB reads only
the row groups a query touches. There is no cache yet, so every request pays the
full round trip -- roughly 8-12 seconds for one country's regions. That is the
known cost of this provider today.

Licensed ODbL, because OpenStreetMap is Overture's primary source. The licence
travels in `provenance` and the renderer puts it in the footnote; that is an
obligation of the licence, not a nicety.
"""

import functools
import json
import os

from mapsvc.cartography import Boundaries, CartographyError, iso

RELEASE = os.environ.get("OVERTURE_RELEASE", "2026-08-19.0")
S3_REGION = "us-west-2"
DIVISION_AREA = (
    "s3://overturemaps-us-west-2/release/{release}"
    "/theme=divisions/type=division_area/*"
)

# Beyond this a stored bounding box may have wrapped the antimeridian. Its
# ordinary xmin/xmax extent then says nothing about where the unit is, so the
# query falls back to the smallest circular arc containing its vertices.
MAX_BBOX_WIDTH = 180

LICENSE = "ODbL 1.0"
ATTRIBUTION = "© OpenStreetMap contributors, © Overture Maps Foundation"

# Canonical level -> Overture subtype. Overture has twelve subtypes; these are
# the four with dependable global coverage. Sub-county coverage is, in Overture's
# own words, "often spotty".
SUBTYPES = {
    "admin_0": "country",
    "admin_1": "region",
    "admin_2": "county",
    "admin_3": "locality",
}
LEVELS = tuple(SUBTYPES)
KEYS = {
    "admin_0": ("ISO3",),
    "admin_1": ("ISO3166_2",),
    "admin_2": (),
    "admin_3": (),
}


@functools.lru_cache(maxsize=1)
def _connection():
    import duckdb

    con = duckdb.connect()
    con.execute("INSTALL spatial; LOAD spatial; INSTALL httpfs; LOAD httpfs;")
    con.execute(f"SET s3_region='{S3_REGION}';")
    # DuckDB draws an ANSI progress bar on stdout, which corrupts a server's
    # log stream and anything piping our output.
    con.execute("SET enable_progress_bar=false;")
    return con


def _intersects(bbox) -> str:
    """SQL for "this unit's bounding box overlaps the window".

    Overture stores per-row bbox columns, so ordinary units take a cheap
    bbox-only path. Only boxes broad enough to have wrapped the antimeridian
    need their geometry vertices inspected.

    Testing `bbox.xmin BETWEEN ...` instead is a tempting mistake: it asks
    whether the unit's *corner* falls inside the window, so a box drawn inside
    Ukraine returns no country at all, Ukraine's own corner being outside it.
    """
    min_lon, min_lat, max_lon, max_lat = bbox
    latitude = f"bbox.ymin <= {max_lat} AND bbox.ymax >= {min_lat}"

    # Every longitude intersects a whole-world window. Besides being simpler,
    # this matters to Overture: referring to `geometry` makes DuckDB fetch that
    # large Parquet column even for a count query.
    if max_lon - min_lon >= 360:
        return latitude

    ordinary = (
        f"bbox.xmax - bbox.xmin < {MAX_BBOX_WIDTH} AND "
        f"bbox.xmin <= {max_lon} AND bbox.xmax >= {min_lon}"
    )
    return f"{latitude} AND (({ordinary}) OR ({_circular_intersects(bbox)}))"


def _circular_intersects(bbox) -> str:
    """SQL overlap test for a bbox whose stored longitude extent has wrapped.

    Overture's bbox has only xmin/xmax, so once it spans nearly 360 degrees the
    true location cannot be recovered from that struct. For those few rows,
    flatten the geometry's vertices, normalise their longitudes onto [0, 360),
    and cut the circle at the largest gap. The complement is the smallest arc
    containing the unit. Two circular arcs overlap when either one's start lies
    in the other.

    The width guard is deliberately inside this expression. It preserves the
    bbox-only fast path for normal units; only antimeridian-wrapped or genuinely
    very broad units undergo the vertex inspection.
    """
    vertices = """SELECT ST_X(point.geom) AS longitude
        FROM unnest(ST_Dump(ST_Points(geometry))) AS points(point)"""
    return (f"bbox.xmax - bbox.xmin >= {MAX_BBOX_WIDTH} AND "
            f"{_smallest_arc_intersects(bbox, vertices)}")


def _smallest_arc_intersects(bbox, vertices: str) -> str:
    """SQL largest-gap overlap, separated so core DuckDB can test it offline.

    ``vertices`` is a query yielding one raw ``longitude`` column. Production
    supplies geometry vertices; tests can supply a small list without loading
    DuckDB's separately distributed spatial extension.
    """
    min_lon, _min_lat, max_lon, _max_lat = bbox
    window_start = (min_lon + 180) % 360
    window_width = max_lon - min_lon
    return f"""EXISTS (
        SELECT 1
        FROM (
            SELECT CASE
                       WHEN gap_index = len(longitudes) THEN longitudes[1]
                       ELSE longitudes[gap_index + 1]
                   END AS arc_start,
                   360 - list_max(gaps) AS arc_width
            FROM (
                SELECT longitudes, gaps,
                       list_position(gaps, list_max(gaps)) AS gap_index
                FROM (
                    SELECT longitudes,
                           list_transform(
                               longitudes,
                               (longitude, i) -> CASE
                                   WHEN i = len(longitudes)
                                       THEN longitudes[1] + 360 - longitude
                                   ELSE longitudes[i + 1] - longitude
                               END
                           ) AS gaps
                    FROM (
                        SELECT list_sort(list_distinct(list(
                                   mod(longitude + 180, 360)
                               ))) AS longitudes
                        FROM ({vertices}) AS vertex_longitudes
                    ) AS vertices
                ) AS measured
            ) AS widest_gap
        ) AS circular_extent
        WHERE mod({window_start} - arc_start + 360, 360) <= arc_width
           OR mod(arc_start - {window_start} + 360, 360) <= {window_width}
    )"""


def _within(codes: list) -> str:
    """SQL for "this unit belongs to one of these".

    A bounding box is spatial, so a window around Volyn also returns Polish and
    Belarusian raions, and a window over the Balkans takes in half of Italy.
    `within` is the political filter a box cannot be: Overture tags every unit
    with its country (ISO2) and, below country level, its parent's ISO 3166-2
    code. Volyn's four raions all report UA-07.

    A list names a region that has no code of its own -- the Balkans is however
    many countries you say it is, which is more honest than a rectangle and
    lets the caller be explicit about the contested memberships.
    """
    regions, countries = [], []
    for code in codes:
        code = code.strip().upper()
        if "-" in code:
            regions.append(code)
            continue
        alpha2 = iso.to_alpha2(code)
        if not alpha2:
            raise CartographyError(
                f"cannot resolve {code!r} to a country. Natural Earth's code "
                "table is the authority here, and it does not use every "
                "three-letter spelling -- Kosovo is KOS or XK, not XKX. A "
                "two-letter code always works.", "within")
        countries.append(alpha2)

    clauses = []
    if regions:
        clauses.append("region IN (" + ", ".join(f"'{_quote(c)}'" for c in regions) + ")")
    if countries:
        clauses.append("country IN (" + ", ".join(f"'{_quote(c)}'" for c in countries) + ")")
    return "(" + " OR ".join(clauses) + ")"


def _where(level: str, bbox, within: str | None = None) -> str:
    if level not in SUBTYPES:
        raise CartographyError(
            f"overture has no {level!r}; it provides {', '.join(SUBTYPES)}", "level"
        )
    # is_land excludes the separate territorial-sea polygon that coastal
    # divisions also carry, which would otherwise duplicate every coastal unit.
    clauses = [f"subtype = '{SUBTYPES[level]}'", "is_land"]
    if bbox is not None:
        clauses.append(_intersects(bbox))
    if within:
        clauses.append(_within(within))
    return " AND ".join(clauses)


def count(level: str, bbox, within: str | None = None) -> int:
    """Units a load would return. Cheap: no geometry leaves S3."""
    sql = (f"SELECT count(*) FROM read_parquet("
           f"'{DIVISION_AREA.format(release=RELEASE)}', hive_partitioning=1) "
           f"WHERE {_where(level, bbox, within)}")
    try:
        return _connection().execute(sql).fetchone()[0]
    except CartographyError:
        raise
    except Exception as exc:
        raise CartographyError(f"overture count failed: {exc}", "basemap.source") from exc


def load(level: str, bbox, detail: str = "simplified",
         within: str | None = None) -> Boundaries:
    sql = f"""
        SELECT id,
               region                          AS iso,
               country                         AS country,
               names.common['en']              AS name_en,
               names.primary                   AS name_primary,
               ST_AsGeoJSON(geometry)          AS geometry
        FROM read_parquet('{DIVISION_AREA.format(release=RELEASE)}',
                          hive_partitioning=1)
        WHERE {_where(level, bbox, within)}
    """
    try:
        rows = _connection().execute(sql).fetchall()
    except CartographyError:
        raise
    except Exception as exc:  # duckdb raises a family of its own errors
        raise CartographyError(f"overture query failed: {exc}", "basemap.source") from exc

    if not rows:
        where = f"in {', '.join(within)}" if within else "in that area"
        raise CartographyError(
            f"overture has no {SUBTYPES[level]} divisions {where}",
            "within" if within else "bbox",
        )

    # `region` is the unit's own ISO 3166-2 code at admin_1, but its *parent's*
    # code at admin_2 and below -- every raion in Vinnytsia Oblast reports
    # UA-05. Using it as an id there would collapse 151 units onto 27. Fall back
    # to Overture's own id whenever it is not unique for this level.
    codes = [row[1] for row in rows]
    codes_are_ids = all(codes) and len(set(codes)) == len(codes)

    units = []
    for oid, unit_iso, country, name_en, name_primary, geometry in rows:
        # names.primary is localised -- Ukrainian oblasts come back in Cyrillic.
        # Prefer the English common name so labels and ids stay legible.
        name = name_en or name_primary or oid
        join_key = (unit_iso if codes_are_ids
                    else (iso.to_alpha3(country) if level == "admin_0" else None))
        units.append({
            "id": unit_iso if codes_are_ids else oid,
            # The join key: ISO 3166-2 where the unit has its own, ISO3 for a
            # country, and nothing below that -- no standard code exists, so
            # statistics cannot be joined at admin_2 and should not pretend to.
            "key": join_key,
            "keys": ({KEYS[level][0]: join_key}
                     if KEYS[level] and join_key else {}),
            "parent": unit_iso if not codes_are_ids else None,
            "name": name,
            "geometry": json.loads(geometry),
        })
    # Countries have no `region`, so at admin_0 every id above is an Overture
    # UUID even though each carries a unique ISO3 join key. The public key is
    # the id a reader (and the eval) can check, so it wins wherever unique --
    # the same rule natural_earth applies to its adm1_code.
    if not codes_are_ids:
        seen: dict = {}
        for unit in units:
            seen[unit["key"]] = seen.get(unit["key"], 0) + 1
        for unit in units:
            if unit["key"] and seen[unit["key"]] == 1:
                unit["id"] = unit["key"]
    units.sort(key=lambda u: (u["name"] or "", u["id"]))

    return Boundaries(units=units, keys=KEYS[level], provenance={
        "source": "Overture Maps",
        "license": LICENSE,
        "attribution": ATTRIBUTION,
        "release": RELEASE,
        "level": level,
        "detail": "full",  # Overture publishes one resolution
        "scale": "divisions theme",
        "keys": list(KEYS[level]),
    })


def _quote(value: str) -> str:
    return value.replace("'", "''")
