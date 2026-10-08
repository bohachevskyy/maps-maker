"""BLS Local Area Unemployment Statistics county annual averages.

One keyless workbook per year contains county FIPS, labor force, employment,
unemployment and the not-seasonally-adjusted unemployment rate.  The newest
completed calendar year is probed rather than pinned.  BLS requires a
descriptive User-Agent with contact information; this provider supplies that
header through :mod:`mapsvc.fetch`.

The workbook is parsed before it is cached, which prevents a BLS HTML denial
page from being saved under an ``.xlsx`` name.
"""

import datetime
import functools
import os
import time
import zipfile

from mapsvc import fetch, xlsx
from mapsvc.statistics import Capabilities, StatisticsError, Values, Variable

SOURCE = "bls"
CAPABILITIES = Capabilities(
    source=SOURCE,
    levels=("admin_2",),
    key={"admin_2": "FIPS"},
    license="public domain",
    attribution="U.S. Bureau of Labor Statistics, Local Area Unemployment Statistics",
    searchable=False,
    time_series=True,
    description=(
        "BLS Local Area Unemployment Statistics (LAUS) county annual "
        "averages: not-seasonally-adjusted unemployment rate, civilian labor "
        "force and unemployed people, keyed by 5-digit county FIPS. Prefer "
        "this source for county unemployment."
    ),
)

URL = "https://www.bls.gov/lau/laucnty{yy:02d}.xlsx"
FIRST_YEAR = 1990
# Measured 2026-10-07: bls.gov returns 403 to anonymous-looking clients. Use a
# descriptive product and durable contact identifier as requested by BLS.
# Measured 2026-10-08: the filter wants an email-shaped contact -- the same
# string with a URL as contact is refused (403), with an address it is served
# (200). Set MAPSVC_CONTACT_EMAIL to a real address you are happy to send.
CONTACT = os.environ.get("MAPSVC_CONTACT_EMAIL", "mapsvc@example.invalid")
BLS_USER_AGENT = f"mapsvc/0.1 (statistical map service; contact: {CONTACT})"
RETRY_DELAYS = (1.0, 2.0, 4.0)
RETRYABLE_STATUS = {403, 429, 500, 502, 503, 504}


def _v(identifier, label, unit, level):
    return Variable(identifier, label, unit, level, year_col="Year",
                    citation="BLS Local Area Unemployment Statistics")


VARIABLES = {"admin_2": {
    "unemployment_rate": _v("unemployment_rate", "Unemployment rate", "%", "ratio"),
    "labor_force": _v("labor_force", "Civilian labor force", "people", "count"),
    "unemployed": _v("unemployed", "Unemployed people", "people", "count"),
}}


def variables(level: str) -> dict:
    return VARIABLES.get(level, {})


def _valid_workbook(payload: bytes) -> bool:
    try:
        book = xlsx.sheets(payload)
    except (zipfile.BadZipFile, KeyError, ValueError):
        return False
    rows = next(iter(book.values()), [])
    return any(row.get("B") == "State FIPS Code"
               and row.get("C") == "County FIPS Code"
               and row.get("I") == "Unemployment Rate (%)" for row in rows)


def _data(year: int) -> bytes:
    name = f"laucnty{year % 100:02d}.xlsx"
    path = fetch.path("bls", name)
    if path.exists():
        payload = path.read_bytes()
        if _valid_workbook(payload):
            return payload
    url = URL.format(yy=year % 100)
    for attempt in range(len(RETRY_DELAYS) + 1):
        try:
            payload = fetch.get(
                url,
                accept="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                user_agent=BLS_USER_AGENT,
            )
            break
        except fetch.FetchError as exc:
            if exc.status == 404:
                raise StatisticsError(
                    f"could not fetch BLS LAUS county data for {year}: {exc}",
                    "variable.change") from exc
            if attempt < len(RETRY_DELAYS) and exc.status in RETRYABLE_STATUS | {None}:
                time.sleep(RETRY_DELAYS[attempt])
                continue
            if exc.status == 403:
                raise StatisticsError(
                    f"BLS denied {url} after {attempt + 1} attempts (HTTP 403 Access Denied)",
                    "variable.source") from exc
            raise StatisticsError(
                f"could not fetch BLS LAUS county data for {year} from {url}: {exc}",
                "variable.source") from exc
    if not _valid_workbook(payload):
        raise StatisticsError(
            f"BLS did not return the {year} LAUS county workbook; response was not cached",
            "variable.source")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".part")
    temporary.write_bytes(payload)
    temporary.replace(path)
    return payload


def _partial_year(payload: bytes) -> bool:
    """True when the workbook's title says it averages fewer than 12 months."""
    rows = next(iter(xlsx.sheets(payload).values()), [])
    title = " ".join(str(v) for row in rows[:4] for v in row.values())
    return "-month" in title.lower() and "12-month" not in title.lower()


@functools.lru_cache(maxsize=1)
def latest_year() -> int:
    for year in range(datetime.date.today().year - 1, FIRST_YEAR - 1, -1):
        try:
            if _partial_year(_data(year)):
                # 2025's file is "11-Month and Annual Averages": October 2025
                # was never collected (the shutdown), so its "annual" figure is
                # an 11-month mean. The latest FULL year is the honest default.
                continue
            return year
        except StatisticsError as exc:
            if exc.field != "variable.change":
                raise
    raise StatisticsError("no BLS LAUS county annual-average workbook found",
                          "variable.source")


@functools.lru_cache(maxsize=8)
def _rows(year: int) -> tuple[dict, ...]:
    book = xlsx.sheets(_data(year))
    rows = next(iter(book.values()), [])
    return tuple(rows)


def load(variable_id: str, level: str, region: str,
         year: int | None = None) -> Values:
    if level != "admin_2":
        raise StatisticsError(f"BLS LAUS county data is admin_2, not {level!r}", "level")
    meta = VARIABLES["admin_2"].get(variable_id)
    if meta is None:
        raise StatisticsError(f"bls has no variable {variable_id!r}", "variable.id")
    chosen = latest_year() if year is None else year
    rows = _rows(chosen)
    column = {"labor_force": "F", "unemployed": "H", "unemployment_rate": "I"}[
        variable_id]
    values = {}
    for row in rows:
        state, county = row.get("B", "").strip(), row.get("C", "").strip()
        if len(state) != 2 or len(county) != 3 or not (state + county).isdigit():
            continue
        try:
            value = float(row.get(column, "").replace(",", ""))
        except ValueError:
            continue
        values[state + county] = value
    if not values:
        raise StatisticsError(f"BLS LAUS had no county values for {chosen}",
                              "variable.change" if year is not None else "variable.source")
    return Values(values=values, years={key: chosen for key in values}, provenance={
        "source": "BLS Local Area Unemployment Statistics",
        "vintage": f"LAUS county {chosen} annual averages",
        "variable": meta.label,
        "unit": meta.unit,
        "level": meta.level,
        "license": CAPABILITIES.license,
        "attribution": CAPABILITIES.attribution,
        "year": str(chosen),
        "key": "FIPS",
        "note": "not seasonally adjusted annual average",
        "zero_is_data": True,
    })
