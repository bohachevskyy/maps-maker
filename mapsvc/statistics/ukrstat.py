"""State Statistics Service of Ukraine: oblast population, 1 January 2022.

A fixed source with one vintage. Ukrstat's population estimate as of 1 January
2022 is the last one published before the full-scale invasion; regional
estimates have been suspended under martial law since. That makes the table
frozen, so it ships with the code rather than being fetched:

    https://ukrstat.gov.ua/operativ/operativ2021/ds/kn/kn_1221_ue.xls
    sha256 c7b050a672f99c8d95c14176f407895ec80d18303a1d673c14068457f440c1de
    "Population (by estimate) as of January 1, 2022", persons

Measured on 2026-10-06: the host serves an expired TLS certificate, and the file
is legacy BIFF .xls, which the standard library cannot read. Vendoring the 26
rows (25 regions plus the national total, which they sum to exactly) is more
honest than disabling certificate checks on every request.

Wikidata was measured as the alternative and rejected for this map: 23 of 27
units carry these same 1 Jan 2022 figures, but Donetsk and Luhansk stop at
2018, Crimea at the 2014 census, and Sevastopol carries a 2022 figure Ukrstat
never published.

Crimea (UA-43) and Sevastopol (UA-40) are in the table with no value: Ukrstat
excludes the territories occupied since 2014, and says so in its footnote. They
are reported as `not_estimated`, not as a failed join.

Open data published under CC BY 4.0 (stated on ukrstat.gov.ua's open-data pages).
"""

import csv
import functools
import pathlib

from mapsvc.statistics import Capabilities, StatisticsError, Values, Variable

SOURCE = "ukrstat"
CAPABILITIES = Capabilities(
    source=SOURCE,
    levels=("admin_1",),
    key={"admin_1": "ISO3166_2"},
    license="CC BY 4.0",
    attribution="State Statistics Service of Ukraine (CC BY 4.0)",
    searchable=False,
    description=(
        "Ukrstat, the State Statistics Service of Ukraine: official population "
        "of Ukraine's 24 oblasts, Kyiv city, Crimea and Sevastopol as of "
        "1 January 2022, the last pre-invasion estimate. Ukraine only, "
        "admin_1 only. Crimea and Sevastopol have no estimate."
    ),
)

DATA = pathlib.Path(__file__).parent / "data" / "ukrstat_population_2022.csv"
YEAR = 2022
VINTAGE = "1 January 2022"
NOT_ESTIMATED = "not_estimated"

VARIABLES = {
    "admin_1": {
        "present_population": Variable(
            id="present_population",
            label="present population",
            unit="people", level="count",
            citation="Ukrstat, Population (by estimate) as of January 1, 2022",
        ),
        "resident_population": Variable(
            id="resident_population",
            label="resident population",
            unit="people", level="count",
            citation="Ukrstat, Population (by estimate) as of January 1, 2022",
        ),
    },
}


def variables(level: str) -> dict:
    return VARIABLES.get(level, {})


@functools.lru_cache(maxsize=1)
def _rows() -> list[dict]:
    with DATA.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def load(variable_id: str, level: str, region: str) -> Values:
    available = variables(level)
    if variable_id not in available:
        raise StatisticsError(
            f"ukrstat offers {', '.join(sorted(available)) or 'nothing'} at "
            f"{level}, not {variable_id!r}", "variable.id")
    meta = available[variable_id]

    values: dict = {}
    missing: dict = {}
    for row in _rows():
        code = row["iso_3166_2"]
        if "-" not in code:
            continue  # the national total
        raw = row[variable_id].strip()
        if raw:
            values[code] = float(raw)
        else:
            missing[code] = NOT_ESTIMATED

    return Values(
        values=values,
        missing=missing,
        years={code: YEAR for code in values},
        provenance={
            "source": "Ukrstat",
            "vintage": VINTAGE,
            "variable": meta.label,
            "unit": meta.unit,
            "level": meta.level,
            "license": CAPABILITIES.license,
            "attribution": CAPABILITIES.attribution,
            "year": VINTAGE,
            "key": "ISO3166_2",
            "reasons": {NOT_ESTIMATED: "not estimated by Ukrstat since 2014 "
                                       "(temporarily occupied)"},
            "note": ("Excludes the temporarily occupied territories of the "
                     "Autonomous Republic of Crimea and Sevastopol (Ukrstat "
                     "publishes no estimate for them)"),
        },
    )
