"""Our World in Data: a searchable source.

OWID publishes thousands of charts, so there is no catalogue here. The agent
supplies a natural-language query, `search` returns ranked candidates, and the
agent picks one of them -- it can name an indicator this module has never heard
of, but not one the search did not return.

Three endpoints, all public and unauthenticated:

    /api/search?q=&type=charts       ranked chart slugs for a query
    /grapher/<slug>.metadata.json    unit, label, type, timespan, citation
    /grapher/<slug>.csv              Entity, Code, Year, value

Licensed CC BY 4.0; the attribution travels in `provenance` to the footnote.
"""

import csv
import datetime
import functools
import io
import json
import pathlib
import urllib.error
import urllib.parse
import urllib.request

from mapsvc.statistics import Candidate, Capabilities, StatisticsError, Values, Variable

SEARCH = "https://ourworldindata.org/api/search"
METADATA = "https://ourworldindata.org/grapher/{slug}.metadata.json"
DATA = "https://ourworldindata.org/grapher/{slug}.csv"

# Without a User-Agent the CDN answers 403.
USER_AGENT = "mapsvc/0.1 (+https://ourworldindata.org)"
FETCH_TIMEOUT = 90

SOURCE = "owid"
CAPABILITIES = Capabilities(
    source=SOURCE,
    levels=("admin_0",),
    key="ISO3",
    license="CC BY 4.0",
    attribution="Our World in Data (CC BY 4.0)",
    searchable=True,
    description=(
        "Our World in Data: thousands of country-level charts on health, "
        "population, energy, environment, economy, education, food, war and "
        "democracy. Country level only. Search it with a plain-English phrase "
        "naming the measure, e.g. 'unemployment rate' or 'deaths from air "
        "pollution'."
    ),
)

# Row 0 is Entity, 1 is Code, 2 is Year; the value is the fourth column.
VALUE_COLUMN = 3

# OWID marks every column Numeric or Ordinal. Numeric is treated as a ratio,
# never a count: a 0 in an OWID series is an observation, not a placeholder.
_LEVELS = {"Numeric": "ratio", "Ordinal": "ordinal", "Categorical": "nominal"}


def _get(url: str, timeout: int = FETCH_TIMEOUT) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise StatisticsError(
                f"Our World in Data has no chart for {url.rsplit('/', 1)[-1]!r}",
                "variable.id",
            ) from exc
        raise StatisticsError(
            f"Our World in Data returned {exc.code}", "variable.source") from exc
    except OSError as exc:
        raise StatisticsError(f"could not reach Our World in Data: {exc}",
                              "variable.source") from exc


def _cache(kind: str, name: str, produce) -> str:
    from mapsvc.harvest import cache_dir

    path = pathlib.Path(cache_dir()) / "owid" / kind / name
    if path.exists():
        return path.read_text()
    text = produce()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".part")
    temporary.write_text(text)
    temporary.replace(path)
    return text


@functools.lru_cache(maxsize=256)
def search(query: str, limit: int = 8) -> list:
    """Ranked chart candidates for a plain-English query.

    Not cached to disk: the catalogue moves, and a stale search is worse than a
    slow one. The in-process cache covers repeats within a run.
    """
    url = SEARCH + "?" + urllib.parse.urlencode(
        {"q": query, "type": "charts", "hitsPerPage": limit})
    payload = json.loads(_get(url, timeout=45))
    return [
        Candidate(
            id=hit["slug"],
            title=hit.get("title", hit["slug"]),
            subtitle=(hit.get("subtitle") or "")[:120],
            coverage=len(hit.get("availableEntities") or []),
        )
        for hit in payload.get("results", [])
        if hit.get("slug")
    ]


def describe(variable_id: str) -> Variable:
    """Measurement metadata for a slug, from OWID rather than from a local dict."""
    payload = json.loads(_cache(
        "metadata", f"{variable_id}.json",
        lambda: _get(METADATA.format(slug=variable_id))))

    columns = payload.get("columns") or {}
    if not columns:
        raise StatisticsError(
            f"{variable_id!r} publishes no data columns", "variable.id")
    name, column = next(iter(columns.items()))
    chart = payload.get("chart") or {}

    return Variable(
        id=variable_id,
        label=column.get("titleShort") or chart.get("title") or name,
        unit=column.get("unit") or None,
        level=_LEVELS.get(column.get("type"), "ratio"),
        timespan=column.get("timespan"),
        citation=chart.get("citation"),
    )


def load(variable_id: str, level: str, region: str) -> Values:
    if level not in CAPABILITIES.levels:
        raise StatisticsError(
            f"Our World in Data publishes by country only, but level is {level!r}",
            "level",
        )

    meta = describe(variable_id)
    text = _cache("data", f"{variable_id}.csv",
                  lambda: _get(DATA.format(slug=variable_id)))
    rows = list(csv.reader(io.StringIO(text)))
    if len(rows) < 2:
        raise StatisticsError(f"{variable_id!r} returned an empty CSV", "variable.id")

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

    values: dict = {}
    for code, (_, raw) in latest.items():
        try:
            values[code.upper()] = float(raw)
        except ValueError:
            values[code.upper()] = raw

    years = {year for year, _ in latest.values()}
    return Values(values=values, provenance={
        "source": "Our World in Data",
        "license": CAPABILITIES.license,
        # CC BY 4.0 obliges crediting Our World in Data; the citation names the
        # upstream study, which is not the same thing. State both.
        "attribution": (f"{meta.citation}, via {CAPABILITIES.attribution}"
                        if meta.citation else CAPABILITIES.attribution),
        "vintage": None,
        "variable": meta.label,
        "unit": meta.unit,
        "level": meta.level,
        "year": str(min(years)) if len(years) == 1 else f"{min(years)}–{max(years)}",
        "key": CAPABILITIES.key,
    })
