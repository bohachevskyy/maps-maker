"""Which rows of a found file the map is about.

A proposal names its rows by column conditions -- `fall = Fell`,
`SIZE_HA >= 200`, `date >= 2011-04-25` -- never by listing values. The same
conditions are applied when the service validates the file, when it draws it
and when an eval scores it, so all three see the same rows.
"""

from __future__ import annotations

OPS = ("eq", "ne", "ge", "le", "gt", "lt", "prefix")
MAX_FILTERS = 3


def normalise(spec) -> list[dict]:
    """[{column, op, value}] from a list, or from the older {column: value} form."""
    if not spec:
        return []
    if isinstance(spec, dict):
        return [{"column": c, "op": "eq", "value": v} for c, v in sorted(spec.items())]
    out = []
    for f in spec:
        if isinstance(f, dict) and f.get("column") and f.get("op", "eq") in OPS:
            out.append({"column": str(f["column"]), "op": f.get("op", "eq"),
                        "value": "" if f.get("value") is None else str(f["value"])})
    return out[:MAX_FILTERS]


def _number(text):
    try:
        return float(str(text).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _holds(cell, op, wanted) -> bool:
    text = "" if cell is None else str(cell).strip()
    want = str(wanted).strip()
    if op == "eq":
        return text.casefold() == want.casefold()
    if op == "ne":
        return text.casefold() != want.casefold()
    if op == "prefix":
        return text.casefold().startswith(want.casefold())
    a, b = _number(text), _number(want)
    if a is None or b is None:          # dates compare as ISO strings
        if not text:
            return False
        a, b = text, want
    return {"ge": a >= b, "le": a <= b, "gt": a > b, "lt": a < b}[op]


def apply(rows: list, spec) -> list:
    filters = normalise(spec)
    if not filters:
        return rows
    return [r for r in rows if all(_holds(r.get(f["column"]), f["op"], f["value"])
                                   for f in filters)]


def describe(spec) -> str:
    """For footnotes: 'fall = Fell; SIZE_HA >= 200'."""
    sign = {"eq": "=", "ne": "≠", "ge": "≥", "le": "≤", "gt": ">", "lt": "<", "prefix": "starts with"}
    return "; ".join(f"{f['column']} {sign[f['op']]} {f['value']}" for f in normalise(spec))
