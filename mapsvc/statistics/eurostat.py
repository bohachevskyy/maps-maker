"""Eurostat regional statistics: a searchable source, keyed by NUTS code.

Two public endpoints, no key:

    /api/dissemination/catalogue/toc/txt              every dataset, with titles
    /api/dissemination/statistics/1.0/data/<code>     JSON-stat 2.0 values

A Eurostat dataset is a cube: besides geo and time it has a unit and often sex,
age or a breakdown. One map is one cell of every other dimension, so a variable
id names the dataset and pins those dimensions as query parameters:

    nama_10r_2gdp?unit=PPS_EU27_2020_HAB    GDP per inhabitant in PPS

`search` ranks the regional datasets in the table of contents by title, reads
each top hit's dimensions from a one-country slice (geo=DE, last period: a few
kB, ~0.5 s), and offers one candidate per unit -- and per value of any other
dimension that has no total. Dimensions with a total (sex=T, age=TOTAL) are
pinned to it. The agent therefore picks a real cell, and cannot invent one.

Only NUTS levels are served: rows are filtered by code length (FR1 is NUTS 1,
FR10 NUTS 2, FR101 NUTS 3) after asking the API for that level (geoLevel).
Licensed CC BY 4.0 (Eurostat's copyright notice: reuse is authorised, the
source must be acknowledged).
"""

import datetime
import functools
import itertools
import json
import re
import urllib.parse

from mapsvc import fetch
from mapsvc.statistics import Candidate, Capabilities, StatisticsError, Values, Variable

SOURCE = "eurostat"
CAPABILITIES = Capabilities(
    source=SOURCE,
    levels=("nuts_1", "nuts_2", "nuts_3"),
    key="NUTS",
    license="CC BY 4.0",
    attribution="Eurostat (CC BY 4.0)",
    searchable=True,
    time_series=True,
    denominators={
        "demo_r_pjangrp3?age=TOTAL&freq=A&sex=T&unit=NR":
            "population on 1 January, every NUTS level, people",
        "reg_area3?freq=A&landuse=TOTAL&unit=KM2": "total area, every NUTS level, km²",
    },
    description=(
        "Eurostat regional statistics for EU, EFTA and candidate countries by "
        "NUTS region (nuts_1, nuts_2, nuts_3): GDP, population, employment and "
        "unemployment, health, education, tourism, transport, agriculture. "
        "Needs the gisco basemap, which carries NUTS codes. Search with the "
        "measure, e.g. 'GDP per inhabitant' or 'unemployment rate'."
    ),
)

API = "https://ec.europa.eu/eurostat/api/dissemination"
TOC = API + "/catalogue/toc/txt?lang=en"
DATA = API + "/statistics/1.0/data/{dataset}"

_CODE_LENGTH = {"nuts_1": 3, "nuts_2": 4, "nuts_3": 5}
# Dimensions every dataset has and a variable never pins.
_FREE = {"geo", "time"}
_TOTALS = ("TOTAL", "T", "ALL")
# How many TOC hits to open, and the most candidates one dataset may offer.
DATASETS_OPENED = 6
PER_DATASET = 12

_STOP = {"the", "of", "a", "an", "in", "by", "for", "and", "to", "per", "eu",
         "europe", "european", "region", "regions", "regional", "nuts", "level",
         "across", "each", "every", "2"}
_SYNONYMS = {"capita": "inhabitant", "person": "inhabitant", "people": "population",
             "jobless": "unemployment", "wealth": "gdp"}


def _get(url: str) -> bytes:
    try:
        return fetch.get(url, timeout=120)
    except fetch.FetchError as exc:
        if exc.status in (400, 404):
            raise StatisticsError(f"Eurostat has no such data: {exc}",
                                  "variable.id") from exc
        raise StatisticsError(f"could not reach Eurostat: {exc}",
                              "variable.source") from exc


def _words(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9]+", text.lower())
    out = set()
    for word in words:
        if word in _STOP:
            continue
        word = _SYNONYMS.get(word, word)
        out.add(word[:-1] if len(word) > 3 and word.endswith("s") else word)
    return out


def _parse_id(variable_id: str) -> tuple[str, dict]:
    dataset, _, query = variable_id.partition("?")
    if not re.fullmatch(r"[a-z0-9_]+", dataset):
        raise StatisticsError(f"{variable_id!r} does not name a Eurostat dataset",
                              "variable.id")
    return dataset, dict(urllib.parse.parse_qsl(query))


def _make_id(dataset: str, pinned: dict) -> str:
    return dataset + ("?" + urllib.parse.urlencode(sorted(pinned.items())) if pinned else "")


# ------------------------------------------------------------------ search ---

@functools.lru_cache(maxsize=1)
def _toc() -> list[tuple[str, str, str, str]]:
    """[(code, title, start, end)] for regional datasets, once per process.

    The table of contents is 2 MB and moves daily; it is fetched fresh per
    process rather than cached to disk, for the same reason OWID search is.
    """
    text = _get(TOC).decode("utf-8", "replace")
    seen, out = set(), []
    for line in text.splitlines()[1:]:
        cells = [c.strip().strip('"').strip() for c in line.split("\t")]
        if len(cells) < 7 or cells[2] not in ("dataset", "table"):
            continue
        title, code = cells[0], cells[1]
        if code in seen:
            continue
        lowered = title.lower()
        if "nuts" not in lowered and "region" not in lowered:
            continue
        seen.add(code)
        out.append((code, title, cells[5], cells[6]))
    return out


@functools.lru_cache(maxsize=64)
def _dimensions(dataset: str) -> dict:
    """{label, dims: [(id, label, [(code, label), ...])]} from a tiny slice."""
    path = fetch.path("eurostat", "dimensions", f"{dataset}.json")
    if path.exists():
        return json.loads(path.read_text())
    payload = json.loads(_get(DATA.format(dataset=dataset)
                              + "?geo=DE&lastTimePeriod=1&lang=en"))
    dims = []
    for dim in payload["id"]:
        if dim in _FREE:
            continue
        category = payload["dimension"][dim]["category"]
        order = sorted(category["index"], key=category["index"].get)
        dims.append((dim, payload["dimension"][dim].get("label", dim),
                     [(code, category.get("label", {}).get(code, code)) for code in order]))
    shaped = {"label": payload.get("label", dataset), "dims": dims}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(shaped))
    return shaped


def _cells(dataset: str, wanted: set) -> list[tuple[tuple, dict, str]]:
    """Candidate cells of one dataset: [(score, pinned, label)]."""
    shape = _dimensions(dataset)
    choices = []
    for dim, _, values in shape["dims"]:
        codes = [code for code, _ in values]
        if dim == "freq":
            choices.append([("A", "annual")] if "A" in codes else values[:1])
        elif dim != "unit" and any(t in codes for t in _TOTALS):
            total = next(t for t in _TOTALS if t in codes)
            choices.append([(total, dict(values)[total])])
        else:
            choices.append(values)
    names = [dim for dim, _, _ in shape["dims"]]
    out = []
    for combo in itertools.islice(itertools.product(*choices), 500):
        pinned = {name: code for name, (code, _) in zip(names, combo)}
        labels = [label for name, (_, label) in zip(names, combo)
                  if name != "freq" and len(dict(shape["dims"][names.index(name)][2])) > 1]
        text = f"{shape['label']} {' '.join(labels)}"
        score = len(wanted & _words(text))
        out.append(((-score, len(text)), pinned, "; ".join(labels)))
    out.sort(key=lambda item: item[0])
    return out[:PER_DATASET]


@functools.lru_cache(maxsize=128)
def search(query: str, limit: int = 8) -> list:
    wanted = _words(query)
    if not wanted:
        return []
    ranked = []
    for position, (code, title, start, end) in enumerate(_toc()):
        words = _words(title)
        hits = len(wanted & words)
        if hits:
            # Most query words, then the title that *starts* with one (Population
            # density before Animal populations), then the shorter title.
            lead = min((title.lower().find(w) for w in wanted if w in title.lower()),
                       default=len(title))
            ranked.append(((-hits, lead > 0, len(title), position), code, title, start, end))
    ranked.sort()

    # Interleave: every opened dataset's best cell before any dataset's second,
    # so one cube with many units or categories cannot fill the whole list.
    candidates = []
    for dataset_rank, (_, code, title, start, end) in enumerate(ranked[:DATASETS_OPENED]):
        try:
            cells = _cells(code, wanted)
        except StatisticsError:
            continue
        for cell_rank, (score, pinned, labels) in enumerate(cells):
            candidates.append(((cell_rank, score, dataset_rank), Candidate(
                id=_make_id(code, pinned), title=title,
                subtitle=f"{labels} ({start}–{end})" if labels else f"{start}–{end}")))
    candidates.sort(key=lambda item: item[0])
    return [candidate for _, candidate in candidates[:limit]]


# ------------------------------------------------------------------ values ---

def _level(unit_code: str, unit_label: str) -> str:
    text = f"{unit_code} {unit_label}".lower()
    if any(s in text for s in ("per ", "percent", "%", "index", "rate", "_hab",
                               "ratio", "average")):
        return "ratio"
    return "count"


def describe(variable_id: str) -> Variable:
    dataset, pinned = _parse_id(variable_id)
    shape = _dimensions(dataset)
    labels = {dim: dict(values) for dim, _, values in shape["dims"]}
    for dim, code in pinned.items():
        if dim not in labels or code not in labels[dim]:
            raise StatisticsError(
                f"{dataset} has no {dim}={code}", "variable.id")
    unit_code = pinned.get("unit", "")
    unit = labels.get("unit", {}).get(unit_code) or None
    if unit_code == "NR" and "population" in shape["label"].lower():
        unit = "people"   # "Number" would make a ratio read "per Number"
    detail = [labels[d][c] for d, c in pinned.items()
              if d not in ("unit", "freq") and len(labels[d]) > 1]
    label, unit_text = human_label(shape["label"], unit, detail)
    return Variable(id=variable_id, label=label, unit=unit_text,
                    level=_level(unit_code, unit or ""), citation=f"Eurostat {dataset}")


def _abbreviate(text: str) -> str:
    """'Gross domestic product (GDP)' -> 'GDP'; 'Purchasing power standard
    (PPS, EU27 from 2020)' -> 'PPS (EU27 from 2020)'.

    Only where the words before the brackets really spell the abbreviation, so
    'Population (persons)' is left alone.
    """
    def replace(match: re.Match) -> str:
        words, abbr, rest = match.group(1).split(), match.group(2), match.group(3)
        for n in range(len(words), 0, -1):
            initials = "".join(w[0] for w in words[-n:]).upper()
            if initials == abbr.upper():
                kept = " ".join(words[:-n])
                tail = f" ({rest.strip()})" if rest else ""
                return f"{kept} {abbr}{tail}".strip()
        return match.group(0)

    return re.sub(r"((?:[A-Za-z][\w-]*\s+){1,6}?)\(([A-Z][A-Z0-9]{1,6})(?:,([^)]*))?\)",
                  lambda m: replace(m) + " ", text).replace("  ", " ").strip()


def human_label(dataset_label: str, unit: str | None, detail: list) -> tuple:
    """A dataset title and a unit label, as a legend heading and its unit line.

    Eurostat's titles describe the table, not the quantity: "Gross domestic
    product (GDP) at current market prices by NUTS 2 region" with the unit
    "Purchasing power standard (PPS, EU27 from 2020), per inhabitant". A map of
    one cell of it shows "GDP per inhabitant" in "PPS (EU27 from 2020)". The
    dataset id stays in the footnote's vintage.
    """
    label = re.sub(r"\s*(,|\band\b|\bby\b)?\s*NUTS\s*\d?\s*regions?\b", "",
                   dataset_label, flags=re.I)
    label = re.sub(r"\s+(by|and)\s*$", "", label.strip(" ,"))
    label = re.sub(r"\s+at current (market )?prices\b", "", label, flags=re.I)
    label = _abbreviate(label)
    text = _abbreviate(unit) if unit else None
    if text:
        match = re.match(r"^(.*?),?\s*\bper (inhabitant|capita|person)\b(.*)$", text, re.I)
        if match:
            label = f"{label} per {match.group(2).lower()}"
            text = " ".join(part for part in (match.group(1).strip(" ,"),
                                              match.group(3).strip(" ,")) if part) or None
            if text and text.lower() in ("euro", "euros"):
                text = "euro"
    if detail:
        label += f" ({', '.join(detail)})"
    return label, text


@functools.lru_cache(maxsize=16)
def _cube(variable_id: str, level: str) -> dict:
    dataset, pinned = _parse_id(variable_id)
    query = dict(pinned, lang="en", geoLevel=level.replace("_", ""))
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", f"{variable_id}_{level}") + ".json"
    path = fetch.path("eurostat", "data", name)
    if path.exists():
        return json.loads(path.read_text())
    payload = json.loads(_get(DATA.format(dataset=dataset) + "?"
                              + urllib.parse.urlencode(sorted(query.items()))))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload))
    return payload


def load(variable_id: str, level: str, region: str, year: int | None = None) -> Values:
    if level not in CAPABILITIES.levels:
        raise StatisticsError(
            f"eurostat is served by NUTS region (nuts_1..nuts_3), not {level!r}", "level")
    meta = describe(variable_id)
    cube = _cube(variable_id, level)

    dims = cube["id"]
    sizes = cube["size"]
    extra = [d for d, n in zip(dims, sizes) if d not in _FREE and n > 1]
    if extra:
        raise StatisticsError(
            f"{variable_id} leaves {', '.join(extra)} unpinned; name one value of "
            "each so the map shows one quantity", "variable.id")
    geo = cube["dimension"]["geo"]["category"]["index"]
    time = cube["dimension"]["time"]["category"]["index"]
    # Strides of the flattened cube, row-major in `dims` order.
    stride, strides = 1, {}
    for dim, size in reversed(list(zip(dims, sizes))):
        strides[dim] = stride
        stride *= size
    observations = cube.get("value", {})
    flags = cube.get("status", {})

    this_year = datetime.date.today().year
    years = sorted((int(t), t) for t in time if t.isdigit() and int(t) <= this_year)
    wanted_length = _CODE_LENGTH[level]
    values, chosen, provisional = {}, {}, 0
    for code, g in geo.items():
        if len(code) != wanted_length or not code[:2].isalpha():
            continue
        for number, label in reversed(years):
            if year is not None and number != year:
                continue
            index = str(g * strides["geo"] + time[label] * strides["time"])
            if index in observations and observations[index] is not None:
                values[code] = float(observations[index])
                chosen[code] = number
                provisional += flags.get(index) in ("p", "e")
                break

    if not values:
        when = f"for {year}" if year is not None else ""
        raise StatisticsError(f"Eurostat {variable_id} has no {level} values {when}".strip(),
                              "variable.change" if year is not None else "variable.id")
    spread = sorted(set(chosen.values()))
    year_label = str(spread[0]) if len(spread) == 1 else f"{spread[0]}–{spread[-1]}"
    provenance = {
        "source": "Eurostat",
        "vintage": f"{_parse_id(variable_id)[0]}, updated {cube.get('updated', '')[:10]}",
        "variable": meta.label,
        "unit": meta.unit,
        "level": meta.level,
        "license": CAPABILITIES.license,
        "attribution": CAPABILITIES.attribution,
        "year": year_label,
        "key": "NUTS",
        "note": (f"{provisional} values provisional or estimated" if provisional else None),
    }
    dataset, pinned = _parse_id(variable_id)
    if pinned.get("unit") == "PPS_EU27_2020_HAB":
        reference_year = max(chosen.values())
        reference = _eu27_reference(dataset, tuple(sorted(pinned.items())), reference_year)
        if reference is not None and reference > 0:
            provenance["index_reference"] = {
                "label": "EU27 = 100", "value": reference,
                "year": reference_year, "raw_unit": meta.unit,
            }
    return Values(values=values, years=chosen, provenance=provenance)


@functools.lru_cache(maxsize=16)
def _eu27_reference(dataset: str, pinned_items: tuple, year: int | None = None) -> float | None:
    """EU27 aggregate for the exact dataset cell and year, when published."""
    pinned = dict(pinned_items)
    query = dict(pinned, geo="EU27_2020", lang="en")
    if year is not None:
        query["time"] = str(year)
    try:
        payload = json.loads(_get(DATA.format(dataset=dataset) + "?"
                                  + urllib.parse.urlencode(sorted(query.items()))))
    except StatisticsError:
        return None
    category = (((payload.get("dimension") or {}).get("geo") or {})
                .get("category") or {})
    geo = category.get("index", {})
    if "EU27_2020" not in geo:
        return None
    observations = payload.get("value") or {}
    if isinstance(observations, list):
        observations = {str(i): value for i, value in enumerate(observations)}
    for value in observations.values():
        if value is not None:
            return float(value)
    return None
