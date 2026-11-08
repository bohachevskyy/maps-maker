"""Datasets the research agent found and the service validated.

A searchable source over the discovered-sources store: each record names a
file, which of its columns is the join key and which the value, plus who
published it and under what licence. Loading reads the file through the same
replayable HTTP layer and the same parser that validated it, so what is drawn
is what was checked. Every map that uses one cites publisher, URL and licence.
"""

from __future__ import annotations

import re

from mapsvc.statistics import Candidate, Capabilities, StatisticsError, Values, Variable

SOURCE = "discovered"

CAPABILITIES = Capabilities(
    source="discovered",
    levels=("admin_0", "admin_1", "admin_2", "nuts_1", "nuts_2", "nuts_3"),
    key={"admin_0": "ISO3", "admin_1": "ISO3166_2", "admin_2": "FIPS",
         "nuts_1": "NUTS", "nuts_2": "NUTS", "nuts_3": "NUTS"},
    license="per dataset (stated in the footnote)",
    attribution="per dataset: publisher and URL in the footnote",
    description=("open datasets found on the web by the research step and validated "
                 "by the service (columns, numeric values, join coverage); searchable"),
    searchable=True,
)

_COUNT_WORDS = ("people", "persons", "inhabitants", "number of", "count", "total")


def _store():
    from mapsvc.discovery.store import Store
    return Store()


def search(query: str, limit: int = 8) -> list:
    out = []
    for rec in _store().search_any(query, geometry_kind="choropleth", limit=limit):
        bits = [b for b in (rec.get("publisher"), rec.get("vintage")) if b]
        out.append(Candidate(id=rec["id"], title=rec.get("title") or rec["url"],
                             subtitle=", ".join(bits)))
    return out


def describe(variable_id: str) -> Variable:
    rec = _record(variable_id)
    unit = rec.get("units") or None
    level = "count" if unit and any(w in unit.lower() for w in _COUNT_WORDS) else "ratio"
    if (rec.get("roles") or {}).get("derive"):
        level = "ratio"            # a derived share or rate is never a count
    return Variable(id=variable_id, label=rec.get("title") or variable_id, unit=unit,
                    level=level, citation=_citation(rec))


def variables(level: str) -> dict:
    return {}     # searchable: nothing is listed up front


def load(variable_id: str, level: str, region=None, year=None) -> Values:
    from mapsvc.discovery import http, probe

    rec = _record(variable_id)
    roles = rec.get("roles") or {}
    try:
        payload = http.get(rec["url"], expect="data").content
    except Exception as exc:  # outage, replay miss, moved file: say which
        raise StatisticsError(f"could not fetch the discovered dataset {rec['url']}: {exc}",
                              "variable.id") from exc
    from mapsvc.discovery import rowfilter
    rows, _fmt, _geo, _warn = probe.records(payload)
    rows = rowfilter.apply(rows, roles.get("filter"))
    scale = float(roles.get("scale") or 1)
    derive = roles.get("derive") or {}

    value_col = roles.get("value")
    key_col = roles.get("key")
    name_col = roles.get("name")
    date_col = roles.get("date")
    if not value_col or not (key_col or name_col):
        raise StatisticsError(f"discovered dataset {variable_id} has no key/value roles",
                              "variable.id")
    from mapsvc.discovery import join
    keys = join.index(rec.get("key_namespace") or "name", rec.get("level") or level)
    by_key = keys.resolver([r.get(key_col) for r in rows]) if key_col else None
    by_name = keys.resolver([r.get(name_col) for r in rows]) if name_col else None
    values: dict = {}
    dated: dict = {}
    for row in rows:
        number = _number(row.get(value_col))
        if number is None:
            continue
        if derive:
            # A ratio the file holds the parts of (e.g. people without health
            # coverage / population x 100); a zero or missing base is no value.
            base = _number(row.get(derive["denominator"]))
            if not base:
                continue
            number = number / base * float(derive.get("factor") or 1)
        key = (by_key(row.get(key_col)) if by_key else None) or (
            by_name(row.get(name_col)) if by_name else None)
        if not key:
            continue
        # Long files hold one row per unit and year: keep each unit's latest.
        when = _when(row.get(date_col)) if date_col else 0
        if when >= dated.get(key, when):
            values[key], dated[key] = number * scale, when
    if not values:
        raise StatisticsError(f"discovered dataset {variable_id} yielded no joinable values",
                              "variable.id")
    _store().mark_used(variable_id)
    return Values(values=values, provenance={
        "source": rec.get("publisher") or "web dataset",
        "variable": (rec.get("title") or variable_id) + (
            f" (computed here: {value_col} / {derive['denominator']}"
            + (f" × {float(derive.get('factor') or 1):g}" if float(derive.get("factor") or 1) != 1 else "")
            + ")" if derive else ""),
        "unit": rec.get("units"),
        "year": rec.get("vintage"),
        "license": rec.get("licence"),
        "attribution": _citation(rec),
        "level": describe(variable_id).level,
        "url": rec["url"],
        "discovered": True,
    })


# --------------------------------------------------------------- helpers ---

def _record(variable_id: str) -> dict:
    rec = _store().get(variable_id)
    if not rec or rec.get("status") != "active":
        raise StatisticsError(f"no active discovered dataset {variable_id!r}", "variable.id")
    return rec


def _citation(rec: dict) -> str:
    """Publisher, title, URL, licence and the date the service validated it.

    The validation date, not today's: output must stay deterministic.
    """
    when = (rec.get("validated_at") or "")[:10]
    return (f"{rec.get('publisher') or 'unknown publisher'}, “{rec.get('title')}”, "
            f"{rec['url']} ({rec.get('licence')}; found by the research step"
            + (f", validated {when}" if when else "") + ")")


def _number(raw):
    text = str(raw if raw is not None else "").replace(",", "").replace(" ", "").strip()
    if text in ("", "..", "-", "NA", "n/a", "null"):
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _when(raw) -> float:
    """A sortable date: '2022', '2022-07-01', 2021.0 -> a number; unknown -> 0."""
    digits = re.findall(r"\d+", str(raw or ""))
    if not digits:
        return 0
    parts = (digits + ["0", "0"])[:3]
    try:
        return int(parts[0]) * 10_000 + int(parts[1]) * 100 + int(parts[2])
    except ValueError:
        return 0
