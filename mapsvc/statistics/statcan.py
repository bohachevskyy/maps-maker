"""Statistics Canada 2021 Census population counts by province/territory.

Table 98-10-0001-01 is a deliberately small official cube: Canada plus its 13
provinces and territories, with comparable 2016 and 2021 Census counts.  The
entire-table download is only a few kilobytes, so there is no need for a
fragile filtered API request.
"""

import csv
import functools
import io
import zipfile

from mapsvc import fetch
from mapsvc.statistics import Capabilities, StatisticsError, Values, Variable

SOURCE = "statcan"
CAPABILITIES = Capabilities(
    source=SOURCE,
    levels=("admin_1",),
    key={"admin_1": "ISO3166_2"},
    license="Open Government Licence - Canada",
    attribution="Statistics Canada, 2021 Census of Population",
    searchable=False,
    time_series=True,
    description=(
        "Statistics Canada Census of Population 2021, Table 98-10-0001-01: "
        "official 2016 and 2021 population counts for Canada's 10 provinces "
        "and 3 territories, keyed by ISO 3166-2. Prefer this source for "
        "Canadian province/territory population change."
    ),
)

DATA = "https://www150.statcan.gc.ca/n1/en/tbl/csv/98100001-eng.zip"
FILENAME = "98100001-eng.zip"
TABLE = "98100001.csv"
POPULATION = "population"

VARIABLES = {"admin_1": {
    POPULATION: Variable(
        POPULATION, "Census population", "people", "count", year_col="REF_DATE",
        timespan="2016–2021", citation="Statistics Canada Table 98-10-0001-01"),
}}

_CODES = {
    "Newfoundland and Labrador": "CA-NL", "Prince Edward Island": "CA-PE",
    "Nova Scotia": "CA-NS", "New Brunswick": "CA-NB", "Quebec": "CA-QC",
    "Ontario": "CA-ON", "Manitoba": "CA-MB", "Saskatchewan": "CA-SK",
    "Alberta": "CA-AB", "British Columbia": "CA-BC", "Yukon": "CA-YT",
    "Northwest Territories": "CA-NT", "Nunavut": "CA-NU",
}


def variables(level: str) -> dict:
    return VARIABLES.get(level, {})


def _csv(payload: bytes) -> str | None:
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            text = archive.read(TABLE).decode("utf-8-sig")
    except (zipfile.BadZipFile, KeyError, UnicodeDecodeError):
        return None
    try:
        header = next(csv.reader(io.StringIO(text)))
    except (StopIteration, csv.Error):
        return None
    needed = ("GEO", "Population, 2021", "Population, 2016")
    return text if all(any(wanted in column for column in header) for wanted in needed) else None


def _data() -> bytes:
    path = fetch.path("statcan", FILENAME)
    if path.exists():
        payload = path.read_bytes()
        if _csv(payload) is not None:
            return payload
    try:
        payload = fetch.get(DATA, accept="application/zip")
    except fetch.FetchError as exc:
        raise StatisticsError(f"could not fetch Statistics Canada census table: {exc}",
                              "variable.source") from exc
    if _csv(payload) is None:
        raise StatisticsError(
            "Statistics Canada did not return Table 98-10-0001-01; response was not cached",
            "variable.source")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".part")
    temporary.write_bytes(payload)
    temporary.replace(path)
    return payload


@functools.lru_cache(maxsize=1)
def _rows() -> tuple[dict, ...]:
    return tuple(csv.DictReader(io.StringIO(_csv(_data()) or "")))


def _column(year: int) -> str:
    needle = f"Population, {year}"
    header = _rows()[0].keys() if _rows() else ()
    found = next((column for column in header if needle in column), None)
    if found is None:
        raise StatisticsError(
            f"Statistics Canada Table 98-10-0001-01 has no population for {year}",
            "variable.change")
    return found


def load(variable_id: str, level: str, region: str,
         year: int | None = None) -> Values:
    if level != "admin_1":
        raise StatisticsError(f"statcan publishes provinces at admin_1, not {level!r}",
                              "level")
    if variable_id != POPULATION:
        raise StatisticsError(f"statcan has no variable {variable_id!r}", "variable.id")
    chosen = 2021 if year is None else year
    column = _column(chosen)
    values = {}
    for row in _rows():
        key = _CODES.get(row.get("GEO", ""))
        if not key:
            continue
        try:
            values[key] = float(row[column])
        except (KeyError, ValueError):
            continue
    if len(values) != 13:
        raise StatisticsError(
            f"Statistics Canada returned {len(values)} of 13 provinces/territories",
            "variable.source")
    # The table's own national row: a figure the map can state without
    # summing anything itself.
    national = next((row for row in _rows() if row.get("GEO") == "Canada"), None)
    aggregates = {}
    if national is not None:
        try:
            aggregates["CA-"] = {"label": "Canada overall", "value": float(national[column])}
        except (KeyError, ValueError):
            pass
    return Values(values=values, years={key: chosen for key in values}, provenance={
        "aggregates": aggregates,
        "source": "Statistics Canada",
        "vintage": "2021 Census of Population, Table 98-10-0001-01",
        "variable": "Census population",
        "unit": "people",
        "level": "count",
        "license": CAPABILITIES.license,
        "attribution": CAPABILITIES.attribution,
        "year": str(chosen),
        "key": "ISO3166_2",
    })
