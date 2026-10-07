"""Wikidata: population and area for any country or ISO 3166-2 subdivision.

One SPARQL query per variable against query.wikidata.org, joined on the codes
Wikidata itself records: P298 (ISO 3166-1 alpha-3) for countries, P300
(ISO 3166-2) for subdivisions. A fixed source over a curated set of
properties, because few Wikidata properties are both quantitative and dense
enough across units to map.

Vintages are mixed, and that is the point to be honest about. Measured
2026-10-06 for population: 4,672 subdivisions have a value; the newest
statement is from the 2020s for 3,217 of them, the 2010s for 1,104, older for
the rest, and 74 carry no date at all. Each unit gets its most recent dated,
non-deprecated statement no later than this year (some statements are
projections), preferred rank first on ties; the footnote states the spread.
For Ukraine the official, single-vintage table is the ukrstat source.

Area uses Wikidata's normalised quantity (square metres), converted to km².
Wikidata is CC0.
"""

import csv
import datetime
import functools
import io
import urllib.parse

from mapsvc import fetch
from mapsvc.statistics import Capabilities, StatisticsError, Values, Variable

SOURCE = "wikidata"
CAPABILITIES = Capabilities(
    source=SOURCE,
    levels=("admin_0", "admin_1"),
    key={"admin_0": "ISO3", "admin_1": "ISO3166_2"},
    license="CC0",
    attribution="Wikidata (CC0)",
    searchable=False,
    time_series=True,
    description=(
        "Wikidata: population and area of countries (admin_0) and first-level "
        "subdivisions anywhere (admin_1, any unit with an ISO 3166-2 code). "
        "Community-maintained, so each unit's figure has its own date -- prefer "
        "an official source when one covers the place."
    ),
)

ENDPOINT = "https://query.wikidata.org/sparql"
_KEY_PROPERTY = {"admin_0": "P298", "admin_1": "P300"}
# id -> (property, value path, unit, factor)
_PROPERTIES = {
    "population": ("P1082", "ps:P1082", "people", 1.0),
    "area": ("P2046", "psn:P2046/wikibase:quantityAmount", "km²", 1e-6),
}
_RANK = {"http://wikiba.se/ontology#PreferredRank": 1,
         "http://wikiba.se/ontology#NormalRank": 0}


def _v(vid, label, unit):
    return Variable(id=vid, label=label, unit=unit, level="count",
                    citation="Wikidata")


_CATALOGUE = {"population": _v("population", "population", "people"),
              "area": _v("area", "area", "km²")}
VARIABLES = {"admin_0": _CATALOGUE, "admin_1": _CATALOGUE}

QUERY = """SELECT ?code ?value ?date ?rank WHERE {{
  ?item wdt:{key} ?code .
  ?item p:{prop} ?st .
  ?st {path} ?value ; wikibase:rank ?rank .
  FILTER(?rank != wikibase:DeprecatedRank)
  OPTIONAL {{ ?st pq:P585 ?date }}
}}"""


def variables(level: str) -> dict:
    return VARIABLES.get(level, {})


@functools.lru_cache(maxsize=8)
def _rows(variable_id: str, level: str) -> tuple:
    prop, path, _, _ = _PROPERTIES[variable_id]
    query = QUERY.format(key=_KEY_PROPERTY[level], prop=prop, path=path)
    url = ENDPOINT + "?" + urllib.parse.urlencode({"query": query})
    try:
        text = fetch.cached(url, "wikidata", f"{variable_id}_{level}.csv",
                            accept="text/csv", timeout=90).decode("utf-8")
    except fetch.FetchError as exc:
        raise StatisticsError(f"Wikidata query failed: {exc}", "variable.source") from exc
    return tuple(csv.DictReader(io.StringIO(text)))


def load(variable_id: str, level: str, region: str, year: int | None = None) -> Values:
    if level not in CAPABILITIES.levels:
        raise StatisticsError(f"wikidata is served at admin_0 and admin_1, not {level!r}",
                              "level")
    if variable_id not in _CATALOGUE:
        raise StatisticsError(
            f"wikidata offers {', '.join(sorted(_CATALOGUE))}, not {variable_id!r}",
            "variable.id")
    _, _, unit, factor = _PROPERTIES[variable_id]
    this_year = datetime.date.today().year

    best: dict = {}
    undated: dict = {}
    for row in _rows(variable_id, level):
        code = row["code"].strip().upper()
        try:
            value = float(row["value"]) * factor
        except ValueError:
            continue
        rank = _RANK.get(row.get("rank", ""), 0)
        date = row.get("date") or ""
        observed = int(date[:4]) if date[:4].isdigit() else None
        if observed is None:
            # Undated (or "unknown value"): used only when nothing dated exists,
            # and never for a specific year.
            if year is None:
                undated.setdefault(code, value)
            continue
        if observed > this_year or (year is not None and observed != year):
            continue
        candidate = (observed, rank, value)
        if code not in best or candidate[:2] > best[code][:2]:
            best[code] = candidate

    values = {code: value for code, (_, _, value) in best.items()}
    years = {code: observed for code, (observed, _, _) in best.items()}
    for code, value in undated.items():
        values.setdefault(code, value)
    if not values:
        raise StatisticsError(
            f"Wikidata has no {variable_id} values"
            + (f" for {year}" if year is not None else ""),
            "variable.change" if year is not None else "variable.id")

    spread = sorted(set(years.values()))
    year_label = (None if not spread else str(spread[0]) if len(spread) == 1
                  else f"{spread[0]}–{spread[-1]}")
    return Values(values=values, years=years, provenance={
        "source": "Wikidata",
        # No retrieval date: it would make the same cached rows render
        # differently from one month to the next. Per-unit years say it.
        "vintage": None,
        "variable": _CATALOGUE[variable_id].label,
        "unit": unit,
        "level": "count",
        "license": CAPABILITIES.license,
        "attribution": CAPABILITIES.attribution,
        "year": year_label,
        "key": CAPABILITIES.key_at(level),
        "note": (f"{len(values) - len(years)} values undated"
                 if len(values) > len(years) else None),
    })
