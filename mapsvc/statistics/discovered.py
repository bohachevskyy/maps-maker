"""Datasets the research agent found and the service validated.

A searchable source over the discovered-sources store: each record names a
file, which of its columns is the join key and which the value, plus who
published it and under what licence. Loading reads the file through the same
replayable HTTP layer and the same parser that validated it, so what is drawn
is what was checked. Every map that uses one cites publisher, URL and licence.
"""

from __future__ import annotations

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
    rows, _fmt, _geo, _warn = probe.records(payload)
    for column, wanted in (roles.get("filter") or {}).items():
        rows = [r for r in rows if str(r.get(column, "")).strip() == str(wanted).strip()]

    value_col = roles.get("value")
    key_col = roles.get("key")
    name_col = roles.get("name")
    if not value_col or not (key_col or name_col):
        raise StatisticsError(f"discovered dataset {variable_id} has no key/value roles",
                              "variable.id")
    aliases = None if key_col else _name_index(level)
    values: dict = {}
    for row in rows:
        raw = row.get(value_col)
        number = _number(raw)
        if number is None:
            continue
        if key_col:
            key = str(row.get(key_col, "")).strip().upper()
        else:
            key = aliases.get(probe._normalise_place(row.get(name_col, "")))
        if key:
            values[key] = number
    if not values:
        raise StatisticsError(f"discovered dataset {variable_id} yielded no joinable values",
                              "variable.id")
    _store().mark_used(variable_id)
    return Values(values=values, provenance={
        "source": rec.get("publisher") or "web dataset",
        "variable": rec.get("title") or variable_id,
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


def _name_index(level: str) -> dict:
    """Normalised unit name -> join key at this level (countries or regions)."""
    from mapsvc.cartography import natural_earth
    from mapsvc.discovery import probe

    boundary_level = "admin_1" if level == "admin_1" else "admin_0"
    units = natural_earth.load(boundary_level, None, "simplified", None).units
    namespace = "ISO3166_2" if boundary_level == "admin_1" else "ISO3"
    index = {}
    for unit in units:
        key = (unit.get("keys") or {}).get(namespace)
        if not key:
            continue
        props = unit.get("properties") or {}
        for name in [unit.get("name")] + [props.get(f) for f in (
                "NAME", "NAME_LONG", "FORMAL_EN", "NAME_EN", "name", "name_en", "name_alt")]:
            if name:
                for variant in str(name).split("|"):
                    index[probe._normalise_place(variant)] = str(key)
    return index
