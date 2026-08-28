"""Our World in Data, through the Grapher CSV API.

Every OWID chart publishes at `ourworldindata.org/grapher/<slug>.csv`, keyed by
ISO3 in a `Code` column -- which is already this service's admin_0 join key, so
no name matching is needed anywhere.

Licensed CC BY 4.0. The attribution travels in `provenance` and the renderer
puts it in the footnote.
"""

import csv
import datetime
import io
import os
import pathlib
import urllib.error
import urllib.request

from mapsvc import registry
from mapsvc.statistics import StatisticsError, Values

BASE = "https://ourworldindata.org/grapher/{slug}.csv"
# Without a User-Agent the CDN answers 403.
USER_AGENT = "mapsvc/0.1 (+https://ourworldindata.org)"
FETCH_TIMEOUT = 90

LICENSE = "CC BY 4.0"
ATTRIBUTION = "Our World in Data"

# Row 0 is Entity, 1 is Code, 2 is Year; the value is the fourth column. Charts
# with several series put the headline one first, which is the one we want.
VALUE_COLUMN = 3


def variables(level: str) -> dict:
    return registry.variables_for(level, "owid")


def _cache_path(slug: str) -> pathlib.Path:
    from mapsvc.harvest import cache_dir

    return cache_dir() / "owid" / f"{slug}.csv"


def _fetch(slug: str) -> str:
    """The whole series for a slug, cached to disk after the first request.

    `csvType=full` is deliberate. The `filtered` variant honours the chart's own
    default country selection -- life-expectancy returns five countries that way
    -- and `time=latest` is ignored on the full export, so the latest year has
    to be chosen here instead.
    """
    path = _cache_path(slug)
    if path.exists():
        return path.read_text()

    request = urllib.request.Request(BASE.format(slug=slug),
                                     headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT) as response:
            text = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        raise StatisticsError(
            f"Our World in Data has no chart {slug!r} ({exc.code})", "variable.id"
        ) from exc
    except OSError as exc:
        raise StatisticsError(f"could not reach Our World in Data: {exc}",
                              "variable.source") from exc

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".part")
    temporary.write_text(text)
    temporary.replace(path)
    return text


def load(variable_id: str, level: str, region: str) -> Values:
    available = variables(level)
    if variable_id not in available:
        if variable_id in registry.OWID_VARIABLES:
            raise StatisticsError(
                f"Our World in Data publishes {variable_id!r} by country only, "
                f"but level is {level!r}", "level"
            )
        raise StatisticsError(
            f"unknown Our World in Data indicator {variable_id!r}; the registry "
            f"holds {', '.join(sorted(registry.OWID_VARIABLES))}", "variable.id"
        )

    meta = available[variable_id]
    rows = list(csv.reader(io.StringIO(_fetch(variable_id))))
    if not rows:
        raise StatisticsError(f"{variable_id!r} returned an empty CSV", "variable.id")

    header = rows[0]
    column = header[VALUE_COLUMN] if len(header) > VALUE_COLUMN else "value"
    # Several OWID series run past the present -- population-density and
    # median-age carry UN projections to 2100. Taking the newest row would
    # quietly map a forecast, so observations are capped at the current year.
    this_year = datetime.date.today().year

    latest: dict[str, tuple[int, str]] = {}
    for row in rows[1:]:
        if len(row) <= VALUE_COLUMN:
            continue
        code, year, value = row[1], row[2], row[VALUE_COLUMN]
        # Aggregate rows (World, Africa, income groups) have no ISO3 or an
        # OWID_ prefix; they are not countries and must not be joined.
        if len(code) != 3 or code.startswith("OWID") or not value.strip():
            continue
        if not year.isdigit() or int(year) > this_year:
            continue
        seen = latest.get(code)
        if seen is None or int(year) > seen[0]:
            latest[code] = (int(year), value)

    if not latest:
        raise StatisticsError(
            f"{variable_id!r} has no country observations at or before {this_year}",
            "variable.id",
        )

    values: dict[str, float | str] = {}
    for code, (_, raw) in latest.items():
        try:
            values[code.upper()] = float(raw)
        except ValueError:
            values[code.upper()] = raw

    years = {year for year, _ in latest.values()}
    return Values(values=values, provenance={
        "source": ATTRIBUTION,
        "license": LICENSE,
        "attribution": f"{ATTRIBUTION} ({LICENSE})",
        "vintage": None,
        "variable": meta.get("label") or variable_id,
        "unit": meta["unit"],
        "level": meta["level"],
        "year": str(min(years)) if len(years) == 1 else f"{min(years)}–{max(years)}",
        "key": "ISO3",
        "column": column,
    })
