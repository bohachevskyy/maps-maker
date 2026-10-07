"""Our World in Data: a searchable source.

OWID publishes thousands of charts, so there is no catalogue here. The agent
supplies a natural-language query, `search` returns ranked candidates, and the
agent picks one of them -- it can name an indicator this module has never heard
of, but not one the search did not return.

Three endpoints, all public and unauthenticated:

    /api/search?q=&type=charts       ranked chart slugs for a query
    /grapher/<slug>.metadata.json    unit, label, type, timespan, citation
    /grapher/<slug>.csv              Entity, Code, Year, value

What the search returns, measured 2026-10-06, and why `search` filters it:

  * Three kinds of hit: `chart`, `multiDimView` and `explorerView`. Explorers
    live under /explorers/ and 404 on /grapher/, so they are dropped.
  * A multi-dimensional view is a slug *plus* query parameters. The bare slug
    resolves to the view's default: "electricity-mix" is total generation in
    TWh, while "share of electricity from renewables" is
    electricity-mix?frequency=annual&metric=share_of_generation&source=renewables
    (the old share-electricity-renewables chart now 301s there). The id keeps
    the parameters, so the agent cannot pick the wrong view by accident.
  * Charts without a WorldMap tab are line or scatter charts of several
    indicators -- the CSV then has several value columns and only the first
    would be mapped. They are dropped too: a map needs a mappable chart.
  * OWID's own order puts explorers and loosely-related charts first, so hits
    are re-ranked by how well the title matches the query.

Licensed CC BY 4.0; the attribution travels in `provenance` to the footnote.
"""

import csv
import datetime
import functools
import io
import json
import pathlib
import re
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
    time_series=True,
    description=(
        "Our World in Data: thousands of country-level charts on health, "
        "population, energy, environment, economy, education, food, war and "
        "democracy, mostly annual time series. Country level only. Search it "
        "with a plain-English phrase naming the measure, e.g. 'unemployment "
        "rate' or 'deaths from air pollution'. For a change between two years "
        "search for the measure itself ('population') and set variable.change."
    ),
)

# Countries OWID gives a code of its own because ISO has none. Kosovo is
# OWID_KOS there and KOS in Natural Earth; without this it is dropped as if it
# were an aggregate like OWID_WRL.
OWID_CODES = {"OWID_KOS": "KOS"}

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


def _split(variable_id: str) -> tuple[str, str]:
    """'electricity-mix?metric=...' -> ('electricity-mix', 'metric=...')."""
    slug, _, query = variable_id.partition("?")
    return slug, query


def _url(template: str, variable_id: str) -> str:
    slug, query = _split(variable_id)
    url = template.format(slug=slug)
    return f"{url}?{query}" if query else url


def _filename(variable_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", variable_id)


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


_STOP = {"the", "of", "a", "an", "in", "by", "for", "and", "from", "to", "per",
         "each", "every", "country", "countries", "world", "share"}
# How many raw hits to ask for: filtering drops explorers and line charts.
_OVERFETCH = 4


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in _STOP}


@functools.lru_cache(maxsize=256)
def search(query: str, limit: int = 8) -> list:
    """Ranked chart candidates for a plain-English query.

    Not cached to disk: the catalogue moves, and a stale search is worse than a
    slow one. The in-process cache covers repeats within a run.
    """
    url = SEARCH + "?" + urllib.parse.urlencode(
        {"q": query, "type": "charts", "hitsPerPage": limit * _OVERFETCH})
    payload = json.loads(_get(url, timeout=45))

    wanted = _words(query)
    ranked, seen = [], set()
    for position, hit in enumerate(payload.get("results", [])):
        slug = hit.get("slug")
        kind = hit.get("type", "chart")
        if not slug or kind not in ("chart", "multiDimView"):
            continue
        if "WorldMap" not in (hit.get("availableTabs") or ["WorldMap"]):
            continue
        params = (hit.get("queryParams") or "").lstrip("?")
        variable_id = f"{slug}?{params}" if kind == "multiDimView" and params else slug
        if variable_id in seen:
            continue
        seen.add(variable_id)
        title = hit.get("title", slug)
        words = _words(title)
        exact = " ".join(sorted(words)) == " ".join(sorted(wanted))
        # Exact title first, then most query words in the title, then fewest
        # words the query did not ask for, then OWID's own order.
        score = (not exact, -len(wanted & words), len(words - wanted), position)
        subtitle = hit.get("variantName") or hit.get("subtitle") or ""
        ranked.append((score, Candidate(
            id=variable_id, title=title, subtitle=subtitle[:120],
            coverage=len(hit.get("availableEntities") or []),
        )))
    ranked.sort(key=lambda pair: pair[0])
    return [candidate for _, candidate in ranked[:limit]]


def describe(variable_id: str) -> Variable:
    """Measurement metadata for a slug, from OWID rather than from a local dict."""
    payload = json.loads(_cache(
        "metadata", f"{_filename(variable_id)}.json",
        lambda: _get(_url(METADATA, variable_id))))

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


def load(variable_id: str, level: str, region: str, year: int | None = None) -> Values:
    if level not in CAPABILITIES.levels:
        raise StatisticsError(
            f"Our World in Data publishes by country only, but level is {level!r}",
            "level",
        )

    meta = describe(variable_id)
    text = _cache("data", f"{_filename(variable_id)}.csv",
                  lambda: _get(_url(DATA, variable_id)))
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
        code, value = row[1], row[VALUE_COLUMN]
        code = OWID_CODES.get(code, code)
        # Aggregate rows (World, Africa, income groups) have no ISO3 or an
        # OWID_ prefix; they are not countries and must not be joined.
        if len(code) != 3 or code.startswith("OWID") or not value.strip():
            continue
        observed = row[2]
        if not re.fullmatch(r"-?\d+", observed) or int(observed) > this_year:
            continue
        if year is not None and int(observed) != year:
            continue
        seen = latest.get(code)
        if seen is None or int(observed) > seen[0]:
            latest[code] = (int(observed), value)

    if not latest:
        when = f"in {year}" if year is not None else f"at or before {this_year}"
        raise StatisticsError(
            f"{variable_id!r} has no country observations {when}",
            "variable.change" if year is not None else "variable.id",
        )

    values: dict = {}
    for code, (_, raw) in latest.items():
        try:
            values[code.upper()] = float(raw)
        except ValueError:
            values[code.upper()] = raw

    years = {observed for observed, _ in latest.values()}
    return Values(values=values,
                  years={code.upper(): observed for code, (observed, _) in latest.items()},
                  provenance={
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
