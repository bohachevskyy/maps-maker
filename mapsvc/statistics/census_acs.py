"""US Census Bureau American Community Survey, 5-year estimates: a fixed source.

Measured 2026-10-06: the Census Data API no longer answers without a key -- a
keyless request is redirected to /data/missing_key.html with the header
X-DataWebAPI-KeyError: 1. So this reads the ACS *table-based summary files*
instead, which are static, keyless and complete:

    https://www2.census.gov/programs-surveys/acs/summary_file/{year}/
        table-based-SF/data/5YRData/acsdt5y{year}-{table}.dat

One pipe-delimited file per table (~18 MB) holding every geography; this module
keeps the states (GEO_ID 0400000USss) and counties (0500000USsssccc) and
drops the rest. Estimates are column `{table}_E{nnn}`, margins `{table}_M{nnn}`.

Variable ids use the API's spelling, B19013_001E, so they read the same as in
every Census document. Values of -666666666 and the other large negative
sentinels mean "no estimate" (too few sample cases) and become missing.

Keyed ISO 3166-2 ("US-CA") at admin_1 and 5-digit county FIPS at admin_2.
"year" is the last year of the 5-year period: 2024 means 2020-2024, dollars
inflation-adjusted to 2024. US government work, public domain.
"""

import datetime
import functools
import re

from mapsvc import fetch, usfips
from mapsvc.statistics import Capabilities, StatisticsError, Values, Variable

SOURCE = "census_acs"
CAPABILITIES = Capabilities(
    source=SOURCE,
    levels=("admin_1", "admin_2"),
    key={"admin_1": "ISO3166_2", "admin_2": "FIPS"},
    license="public domain",
    attribution="U.S. Census Bureau, American Community Survey 5-year estimates",
    searchable=False,
    time_series=True,
    description=(
        "US Census Bureau American Community Survey 5-year estimates: income, "
        "poverty, housing, education, employment and population for US states "
        "(admin_1) and counties (admin_2). US only. Counties are keyed by FIPS, "
        "so admin_2 needs the census basemap. Divide a count by its universe "
        "with variable.per to get a rate (B17001_002E per B17001_001E is the "
        "poverty rate)."
    ),
)

URL = ("https://www2.census.gov/programs-surveys/acs/summary_file/{year}/"
       "table-based-SF/data/5YRData/acsdt5y{year}-{table}.dat")
# Table-based summary files start with the 2021 release; earlier years use a
# sequence-based layout this module does not read.
FIRST_YEAR = 2021
SENTINEL = -111111111   # every Census no-estimate code is below this

_PREFIX = {"admin_1": "0400000US", "admin_2": "0500000US"}


def _v(vid, label, unit, level):
    return Variable(id=vid, label=label, unit=unit, level=level,
                    citation="U.S. Census Bureau, ACS 5-year estimates")


_DOLLARS = "US dollars, inflation-adjusted"
CATALOGUE = {
    "B19013_001E": _v("B19013_001E", "median household income", _DOLLARS, "ratio"),
    "B19301_001E": _v("B19301_001E", "per capita income", _DOLLARS, "ratio"),
    "B19083_001E": _v("B19083_001E", "Gini index of income inequality", "index", "ratio"),
    "B01003_001E": _v("B01003_001E", "total population", "people", "count"),
    "B01002_001E": _v("B01002_001E", "median age", "years", "ratio"),
    "B11001_001E": _v("B11001_001E", "households", "households", "count"),
    "B17001_001E": _v("B17001_001E", "population for whom poverty status is determined",
                      "people", "count"),
    "B17001_002E": _v("B17001_002E", "people with income below the poverty level",
                      "people", "count"),
    "B23025_003E": _v("B23025_003E", "civilian labor force", "people", "count"),
    "B23025_005E": _v("B23025_005E", "unemployed (civilian labor force)", "people", "count"),
    "B15003_001E": _v("B15003_001E", "population 25 years and over", "people", "count"),
    "B15003_022E": _v("B15003_022E", "population 25+ with a bachelor's degree",
                      "people", "count"),
    "B25077_001E": _v("B25077_001E", "median value of owner-occupied housing",
                      _DOLLARS, "ratio"),
    "B25064_001E": _v("B25064_001E", "median gross rent", "US dollars per month", "ratio"),
    "B25003_001E": _v("B25003_001E", "occupied housing units", "housing units", "count"),
    "B25003_002E": _v("B25003_002E", "owner-occupied housing units", "housing units", "count"),
}
VARIABLES = {"admin_1": CATALOGUE, "admin_2": CATALOGUE}


def variables(level: str) -> dict:
    return VARIABLES.get(level, {})


def _column(variable_id: str) -> tuple[str, str]:
    """'B19013_001E' -> ('b19013', 'B19013_E001')."""
    match = re.fullmatch(r"([A-Z]\d{5}[A-Z]{0,3})_(\d{3})E", variable_id)
    if not match:
        raise StatisticsError(f"{variable_id!r} is not an ACS estimate id", "variable.id")
    table, number = match.groups()
    return table.lower(), f"{table}_E{number}"


def _table(table: str, year: int) -> str:
    try:
        payload = fetch.cached(URL.format(year=year, table=table),
                               "census_acs", str(year), f"{table}.dat")
    except fetch.FetchError as exc:
        # census.gov answers 401 (not 404) for a release that is not published
        # yet -- measured 2026-11-24 for the 2025 vintage.
        if exc.status in (401, 403, 404):
            raise StatisticsError(
                f"no ACS 5-year {year} summary file for table {table.upper()}",
                "variable.change") from exc
        raise StatisticsError(f"could not fetch ACS: {exc}", "variable.source") from exc
    return payload.decode("utf-8", "replace")


@functools.lru_cache(maxsize=8)
def latest_year(table: str) -> int:
    """The newest 5-year release on the server, newest first.

    Releases arrive each December/January, so the year is probed rather than
    pinned: a constant here would quietly go stale.
    """
    this_year = datetime.date.today().year
    for year in range(this_year - 1, FIRST_YEAR - 1, -1):
        try:
            _table(table, year)
            return year
        except StatisticsError as exc:
            if exc.field != "variable.change":
                raise
    raise StatisticsError(f"no ACS 5-year summary file found for {table.upper()}",
                          "variable.id")


def load(variable_id: str, level: str, region: str, year: int | None = None) -> Values:
    if level not in CAPABILITIES.levels:
        raise StatisticsError(
            f"census_acs publishes states (admin_1) and counties (admin_2), "
            f"not {level!r}", "level")
    meta = CATALOGUE.get(variable_id)
    if meta is None:
        raise StatisticsError(
            f"census_acs offers {', '.join(sorted(CATALOGUE))}, not {variable_id!r}",
            "variable.id")
    if year is not None and year < FIRST_YEAR:
        raise StatisticsError(
            f"ACS summary files are read from {FIRST_YEAR} on, not {year}",
            "variable.change")
    table, column = _column(variable_id)
    year = latest_year(table) if year is None else year
    lines = _table(table, year).splitlines()

    header = lines[0].split("|")
    if column not in header:
        raise StatisticsError(f"{column} is not in ACS table {table.upper()}",
                              "variable.id")
    index = header.index(column)
    prefix = _PREFIX[level]

    values, missing = {}, {}
    for line in lines[1:]:
        if not line.startswith(prefix):
            continue
        cells = line.split("|")
        code = cells[0][len(prefix):]
        key = usfips.iso_3166_2(code) if level == "admin_1" else code
        if key is None:
            continue
        try:
            value = float(cells[index])
        except (ValueError, IndexError):
            missing[key] = "no_data"
            continue
        if value <= SENTINEL:
            missing[key] = "no_data"
        else:
            values[key] = value

    unit = meta.unit
    if unit == _DOLLARS:
        unit = f"{year} US dollars"
    period = f"{year - 4}–{year}"
    return Values(
        values=values, missing=missing, years={k: year for k in values},
        provenance={
            "source": "US Census Bureau ACS",
            "vintage": f"ACS 5-year {period}",
            "variable": meta.label,
            "unit": unit,
            "level": meta.level,
            "license": CAPABILITIES.license,
            "attribution": CAPABILITIES.attribution,
            "year": f"{period} (ACS 5-year)",
            "key": CAPABILITIES.key_at(level),
            "zero_is_data": True,
        },
    )
