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


def load(level: str, region: str, detail: str = "simplified") -> Boundaries:
    if level not in SUBTYPES:
        raise CartographyError(
            f"overture has no {level!r}; it provides {', '.join(SUBTYPES)}", "level"
        )
    subtype = SUBTYPES[level]

    where = [f"subtype = '{subtype}'", "is_land"]
    wanted = region.strip().lower()
    if wanted != "world":
        # Overture divisions carry a country code and no continent, so a
        # continent has to be expanded into its member countries first.
        members = iso.countries_in(region)
        if members:
            listed = ", ".join(f"'{_quote(c)}'" for c in members)
            where.append(f"country IN ({listed})")
        else:
            alpha2 = iso.to_alpha2(region)
            if not alpha2:
                raise CartographyError(
                    f"cannot resolve region {region!r}; expected 'world', a "
                    "CONTINENT name, or a country code", "region"
                )
            where.append(f"country = '{_quote(alpha2)}'")
    elif level != "admin_0":
        raise CartographyError(
            f"region 'world' at {level} would pull every division on earth from "
            "S3 with no cache in front of it; name a country instead", "region"
        )

    # is_land excludes the separate territorial-sea polygon that coastal
    # divisions also carry, which would otherwise duplicate every coastal unit.
    sql = f"""
        SELECT id,
               region                          AS iso,
               country                         AS country,
               names.common['en']              AS name_en,
               names.primary                   AS name_primary,
               ST_AsGeoJSON(geometry)          AS geometry
        FROM read_parquet('{DIVISION_AREA.format(release=RELEASE)}',
                          hive_partitioning=1)
        WHERE {' AND '.join(where)}
    """
    try:
        rows = _connection().execute(sql).fetchall()
    except Exception as exc:  # duckdb raises a family of its own errors
        raise CartographyError(f"overture query failed: {exc}", "basemap.source") from exc

    if not rows:
        raise CartographyError(
            f"overture returned no {subtype} divisions for region {region!r}", "region"
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
        units.append({
            "id": unit_iso if codes_are_ids else oid,
            # The join key: ISO 3166-2 where the unit has its own, ISO3 for a
            # country, and nothing below that -- no standard code exists, so
            # statistics cannot be joined at admin_2 and should not pretend to.
            "key": (unit_iso if codes_are_ids
                    else (iso.to_alpha3(country) if level == "admin_0" else None)),
            "parent": unit_iso if not codes_are_ids else None,
            "name": name,
            "geometry": json.loads(geometry),
        })
    units.sort(key=lambda u: (u["name"] or "", u["id"]))

    return Boundaries(units=units, provenance={
        "source": "Overture Maps",
        "license": LICENSE,
        "attribution": ATTRIBUTION,
        "release": RELEASE,
        "level": level,
        "detail": "full",  # Overture publishes one resolution
        "scale": "divisions theme",
    })


def _quote(value: str) -> str:
    return value.replace("'", "''")
