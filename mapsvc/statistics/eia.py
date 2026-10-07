"""EIA State Energy Data System (SEDS): searchable, US states, annual since 1960.

Two public files, no API key:

    /state/seds/CDF/Complete_SEDS.zip            every series, state and year
    /state/seds/CDF/Codes_and_Descriptions.xlsx  what each series code means

A series is a five-character MSN: DFTCP is distillate fuel oil (D F) total
consumption (T C) in physical units (P). The catalogue is ~970 series, too
many to list to the agent and too few to need a remote search, so `search`
ranks the code list locally.

Values are converted out of SEDS's "thousand" units at load -- DFTCP arrives in
thousand barrels and TPOPP in thousand people -- so that a per-capita ratio
reads "barrels per person" rather than "thousand barrels per thousand".

Keyed by ISO 3166-2 ("US-TX"): the 50 states and DC. The US total and the two
federal offshore regions (X3, X5) are not states and are not joined.

US government work, public domain.
"""

import csv
import functools
import io
import re
import zipfile

from mapsvc import fetch, xlsx
from mapsvc.statistics import Candidate, Capabilities, StatisticsError, Values, Variable

SOURCE = "eia"
CAPABILITIES = Capabilities(
    source=SOURCE,
    levels=("admin_1",),
    key={"admin_1": "ISO3166_2"},
    license="public domain",
    attribution="U.S. Energy Information Administration, State Energy Data System",
    searchable=True,
    time_series=True,
    denominators={"TPOPP": "resident population of each state, people"},
    description=(
        "EIA State Energy Data System: energy consumption, production, prices, "
        "expenditures and CO2 emissions by fuel and sector for the 50 US states "
        "and DC, annually since 1960, plus resident population (TPOPP) and GDP. "
        "US states only (admin_1). Search with the fuel and measure, e.g. "
        "'distillate fuel oil consumption' -- diesel is distillate fuel oil."
    ),
)

DATA = "https://www.eia.gov/state/seds/CDF/Complete_SEDS.zip"
CODES = "https://www.eia.gov/state/seds/CDF/Codes_and_Descriptions.xlsx"

# Postal codes that are not states: the national total and federal offshore.
NOT_STATES = {"US", "X3", "X5"}
STATES = 51

# Words a user says that SEDS does not.
SYNONYMS = {
    "diesel": "distillate", "petrol": "gasoline", "power": "electricity",
    "people": "population", "inhabitants": "population", "emissions": "co2",
    "carbon": "co2", "fuel-oil": "distillate", "heating": "distillate",
}
STOPWORDS = {"the", "of", "in", "by", "per", "and", "a", "an", "for", "us",
             "u.s.", "state", "states", "across", "each", "every", "american",
             "use", "used", "how", "much"}


def _get(url: str, *parts: str) -> bytes:
    try:
        return fetch.cached(url, "eia", *parts)
    except fetch.FetchError as exc:
        raise StatisticsError(f"could not fetch EIA SEDS: {exc}",
                              "variable.source") from exc


@functools.lru_cache(maxsize=1)
def catalogue() -> dict[str, tuple[str, str]]:
    """{MSN: (description, unit)} from EIA's own code list."""
    book = xlsx.sheets(_get(CODES, "Codes_and_Descriptions.xlsx"))
    rows = book.get("MSN descriptions") or next(iter(book.values()))
    out = {}
    for row in rows:
        code, description = row.get("B", "").strip(), row.get("C", "").strip()
        if re.fullmatch(r"[A-Z0-9]{5}", code) and description:
            out[code] = (description, row.get("D", "").strip())
    if not out:
        raise StatisticsError("EIA's code list had no series in it", "variable.source")
    return out


def _scale(description: str, unit: str) -> tuple[float, str]:
    """(factor, unit) taking SEDS's "thousand" units to plain ones."""
    if unit == "Thousand":
        return 1000.0, "people" if "population" in description.lower() else ""
    match = re.fullmatch(r"Thousands? (?:of )?(.+)", unit)
    if match and " per " not in unit:
        return 1000.0, match.group(1)
    return 1.0, unit


def _level(description: str, unit: str) -> str:
    text = f"{description} {unit}".lower()
    if (" per " in text or "percent" in text or "price" in text
            or "per capita" in text or unit.lower() == "days"):
        return "ratio"
    return "count"


def describe(variable_id: str) -> Variable:
    found = catalogue().get(variable_id)
    if found is None:
        raise StatisticsError(f"EIA SEDS has no series {variable_id!r}", "variable.id")
    description, unit = found
    _, plain = _scale(description, unit)
    level = _level(description, unit)
    if plain and level == "count" and re.search(r"consumption|production|expenditures",
                                                description, re.I):
        # SEDS flows are annual totals; a bare "barrels" leaves the reader
        # guessing the period.
        plain = f"{plain} per year"
    return Variable(id=variable_id, label=description, unit=plain or None,
                    level=level,
                    citation="EIA State Energy Data System")


def _note(description: str) -> str | None:
    """Reader-facing fuel meaning derived from EIA's series description."""
    if "distillate fuel oil" in description.casefold():
        return "distillate fuel oil: diesel and heating oil"
    return None


def _stem(word: str) -> str:
    return word[:-1] if len(word) > 3 and word.endswith("s") and not word.endswith("ss") else word


def _tokens(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9][a-z0-9.\-]*", text.lower())
    return {_stem(w) for w in words if w not in STOPWORDS}


@functools.lru_cache(maxsize=256)
def search(query: str, limit: int = 8) -> list:
    said = _tokens(query)
    # A user's word and SEDS's word for it both count, but SEDS's own word
    # ranks first: "diesel" in SEDS means renewable diesel only, while a person
    # asking about diesel means distillate fuel oil.
    meant = {SYNONYMS[w] for w in said if w in SYNONYMS}
    wanted = said | meant
    if not wanted:
        return []
    scored = []
    for code, (description, unit) in catalogue().items():
        words = _tokens(f"{description} {unit}")
        hits = len(wanted & words)
        if not hits:
            continue
        # Ties: a total before a sector, physical units before Btu (which is
        # what a reader means by "consumption"), then the shorter description.
        score = (hits,
                 len(meant & words),
                 "total" in words,
                 code.endswith("P") and "btu" not in wanted,
                 -len(description))
        scored.append((score, code, description, unit))
    scored.sort(key=lambda s: (s[0], s[1]), reverse=True)
    return [Candidate(id=code, title=description, subtitle=f"{unit}; MSN {code}",
                      coverage=STATES)
            for _, code, description, unit in scored[:limit]]


@functools.lru_cache(maxsize=32)
def _series(variable_id: str) -> tuple[dict, str]:
    """({state: {year: value}}, data status) for one series."""
    path = fetch.path("eia", "Complete_SEDS.zip")
    if not path.exists():
        _get(DATA, "Complete_SEDS.zip")
    series: dict = {}
    status = ""
    with zipfile.ZipFile(path) as archive:
        name = next(n for n in archive.namelist() if n.lower().endswith(".csv"))
        with archive.open(name) as handle:
            for row in csv.reader(io.TextIOWrapper(handle, encoding="utf-8")):
                if len(row) < 5 or row[1] != variable_id:
                    continue
                status, state, year, value = row[0], row[2], row[3], row[4]
                if state in NOT_STATES or not year.isdigit():
                    continue
                try:
                    series.setdefault(state, {})[int(year)] = float(value)
                except ValueError:
                    continue
    return series, status


def load(variable_id: str, level: str, region: str, year: int | None = None) -> Values:
    if level not in CAPABILITIES.levels:
        raise StatisticsError(
            f"EIA SEDS publishes by US state only, but level is {level!r}", "level")
    meta = describe(variable_id)
    description, unit = catalogue()[variable_id]
    factor, _ = _scale(description, unit)

    series, status = _series(variable_id)
    if not series:
        raise StatisticsError(f"EIA SEDS has no state data for {variable_id}",
                              "variable.id")
    available = sorted({y for by_year in series.values() for y in by_year})
    chosen = available[-1] if year is None else year
    if chosen not in available:
        raise StatisticsError(
            f"EIA SEDS {variable_id} covers {available[0]}–{available[-1]}, "
            f"not {chosen}", "variable.change")

    values = {f"US-{state}": by_year[chosen] * factor
              for state, by_year in series.items() if chosen in by_year}
    return Values(
        values=values,
        years={key: chosen for key in values},
        provenance={
            "source": "EIA SEDS",
            "vintage": f"SEDS release {status}" if status else None,
            "variable": meta.label,
            "unit": meta.unit,
            "note": _note(description),
            "level": meta.level,
            "license": CAPABILITIES.license,
            "attribution": CAPABILITIES.attribution,
            "year": str(chosen),
            "key": "ISO3166_2",
            # A state that burns no coal reports 0, and that is a fact.
            "zero_is_data": True,
        },
    )
