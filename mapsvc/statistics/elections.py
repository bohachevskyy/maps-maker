"""MIT Election Data + Science Lab presidential returns, by US state.

The Harvard Dataverse DOI resolves to a versioned dataset, but its data-file
API URL is stable for this released CSV.  Rows are candidate results; the
denominator is ``totalvotes`` for the whole state popular vote, not the sum of
the two major parties.  Party rows are summed before shares are calculated so
the parser remains correct if a party has more than one ballot line.

The download is checked for the exact CSV header before it is cached.  Harvard
occasionally sends an HTML interstitial on an error, and treating that as an
empty election would be much worse than failing explicitly.
"""

import csv
import datetime
import functools
import io

from mapsvc import fetch
from mapsvc.statistics import Capabilities, StatisticsError, Values, Variable

SOURCE = "elections"
CAPABILITIES = Capabilities(
    source=SOURCE,
    levels=("admin_1",),
    key={"admin_1": "ISO3166_2"},
    license="CC0 1.0",
    attribution="MIT Election Data + Science Lab (CC0 1.0)",
    searchable=False,
    time_series=True,
    description=(
        "MIT Election Data + Science Lab U.S. presidential popular-vote "
        "returns, 1976–2024, for the 50 states and DC (admin_1). Offers "
        "Republican and Democratic shares of total votes and Republican "
        "minus Democratic margin in percentage points."
    ),
)

DATA = "https://dataverse.harvard.edu/api/access/datafile/13887042"
FILENAME = "1976-2024-president.csv"
EXPECTED = {"year", "state_po", "candidate", "party_simplified",
            "candidatevotes", "totalvotes"}


def _v(identifier: str, label: str, unit: str) -> Variable:
    return Variable(identifier, label, unit, "ratio", year_col="year",
                    timespan="1976–2024",
                    citation="MIT Election Data + Science Lab, U.S. President 1976–2024")


VARIABLES = {"admin_1": {
    "republican_share": _v(
        "republican_share", "Republican share of presidential popular vote",
        "% of total popular vote"),
    "democratic_share": _v(
        "democratic_share", "Democratic share of presidential popular vote",
        "% of total popular vote"),
    "republican_minus_democratic_margin": _v(
        "republican_minus_democratic_margin",
        "Presidential popular-vote margin",
        "percentage points (Republican minus Democratic; positive = Republican lead)"),
}}


def variables(level: str) -> dict:
    return VARIABLES.get(level, {})


def _valid_csv(payload: bytes) -> bool:
    if payload.lstrip().lower().startswith((b"<!doctype html", b"<html")):
        return False
    try:
        header = next(csv.reader(io.StringIO(payload.decode("utf-8-sig"))))
    except (UnicodeDecodeError, StopIteration, csv.Error):
        return False
    return EXPECTED <= set(header)


def _data() -> bytes:
    path = fetch.path("elections", FILENAME)
    if path.exists():
        payload = path.read_bytes()
        if _valid_csv(payload):
            return payload
    try:
        payload = fetch.get(DATA, accept="text/csv")
    except fetch.FetchError as exc:
        raise StatisticsError(f"could not fetch MIT Election Lab returns: {exc}",
                              "variable.source") from exc
    if not _valid_csv(payload):
        raise StatisticsError(
            "Harvard Dataverse did not return the presidential-results CSV; "
            "the response was not cached", "variable.source")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".part")
    temporary.write_bytes(payload)
    temporary.replace(path)
    return payload


@functools.lru_cache(maxsize=1)
def _series() -> dict[int, dict[str, dict]]:
    years: dict[int, dict[str, dict]] = {}
    text = _data().decode("utf-8-sig")
    for row in csv.DictReader(io.StringIO(text)):
        try:
            year = int(row["year"])
            votes = float(row["candidatevotes"])
            total = float(row["totalvotes"])
        except (KeyError, TypeError, ValueError):
            continue
        postal = row.get("state_po", "").strip().upper()
        if len(postal) != 2 or total <= 0:
            continue
        state = years.setdefault(year, {}).setdefault(
            postal, {"total": total, "REPUBLICAN": 0.0, "DEMOCRAT": 0.0})
        state["total"] = total
        party = row.get("party_simplified", "").strip().upper()
        if party in ("REPUBLICAN", "DEMOCRAT"):
            state[party] += votes
    return years


def load(variable_id: str, level: str, region: str,
         year: int | None = None) -> Values:
    if level != "admin_1":
        raise StatisticsError(
            f"elections publishes US states and DC (admin_1), not {level!r}", "level")
    meta = VARIABLES["admin_1"].get(variable_id)
    if meta is None:
        raise StatisticsError(f"elections has no variable {variable_id!r}", "variable.id")
    series = _series()
    available = sorted(y for y in series if y <= datetime.date.today().year)
    chosen = available[-1] if year is None and available else year
    if chosen not in series:
        span = f"{available[0]}–{available[-1]}" if available else "no years"
        raise StatisticsError(f"MIT presidential returns cover {span}, not {chosen}",
                              "variable.change" if year is not None else "variable.id")

    values = {}
    for postal, row in series[chosen].items():
        republican = 100.0 * row["REPUBLICAN"] / row["total"]
        democratic = 100.0 * row["DEMOCRAT"] / row["total"]
        value = {"republican_share": republican,
                 "democratic_share": democratic,
                 "republican_minus_democratic_margin": republican - democratic}[variable_id]
        values[f"US-{postal}"] = value

    provenance = {
        "source": "MIT Election Data + Science Lab",
        "vintage": "U.S. President 1976–2024, Harvard Dataverse v10.1",
        "variable": meta.label,
        "unit": meta.unit,
        "level": "ratio",
        "license": CAPABILITIES.license,
        "attribution": CAPABILITIES.attribution,
        "year": str(chosen),
        "key": "ISO3166_2",
        "note": "Republican and Democratic candidates; state popular vote, not electoral votes",
        "zero_is_data": True,
    }
    if variable_id == "republican_minus_democratic_margin":
        provenance["diverging"] = {
            "negative": "Democratic lead", "positive": "Republican lead",
            "negative_color": "blue", "positive_color": "red",
        }
    return Values(values=values, years={key: chosen for key in values},
                  provenance=provenance)
