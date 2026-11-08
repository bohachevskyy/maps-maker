"""Turning a column of codes or names from a found file into our boundary keys.

Validation and drawing share this, so the coverage a dataset was accepted on
is the coverage it is drawn with. Files key countries every which way -- ISO3,
ISO2, UN M49 numbers (FAO: "Afghanistan,4,..."), or plain names -- and
subdivisions by ISO 3166-2, HASC, postal suffix or name. Each value is
resolved against the real boundary units, never guessed.
"""

from __future__ import annotations

import dataclasses
import re
from collections import Counter

_COUNTRY_CODES = ("ISO_A3", "ISO_A3_EH", "ADM0_A3", "ADM0_ISO", "WB_A3",
                  "ISO_A2", "ISO_A2_EH", "WB_A2")
_COUNTRY_NUMBERS = ("ISO_N3", "ISO_N3_EH", "UN_A3")
_COUNTRY_NAMES = ("NAME", "NAME_LONG", "FORMAL_EN", "NAME_EN", "NAME_SORT", "ADMIN",)
_REGION_NAMES = ("name", "name_en", "woe_name", "gn_name", "name_alt", "name_local")


def normalise_name(value) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value).casefold()).strip()


def _token(value) -> str | None:
    """A lookup token: numbers, short codes and names never collide."""
    text = str(value if value is not None else "").strip()
    if not text or text in ("-99", "-", ".."):
        return None
    if re.fullmatch(r"\d+(\.0+)?", text):
        return "N:" + str(int(float(text)))
    if re.fullmatch(r"[A-Za-z]{2,3}|[A-Za-z]{2}[-.][A-Za-z0-9]{1,3}", text):
        return "C:" + text.upper().replace(".", "-")
    return "T:" + normalise_name(text)


@dataclasses.dataclass
class Index:
    namespace: str
    lookup: dict          # token -> canonical key
    units: dict           # canonical key -> unit (for scope by country)
    ambiguous: dict = dataclasses.field(default_factory=dict)   # token -> {keys}

    def resolve(self, value) -> str | None:
        token = _token(value)
        return self.lookup.get(token) if token else None

    def resolver(self, values):
        """`resolve`, plus ambiguous tokens settled by where the rest of the data is.

        "Distrito Federal" is Brazil's and Mexico's; "SP" is a postal code in
        several countries. In a file whose other rows resolve to Brazil, both
        mean Brazil's unit.
        """
        firm = [k for k in (self.resolve(v) for v in values) if k]
        if not self.ambiguous or not firm:
            return self.resolve
        counts = Counter(k.split("-")[0] for k in firm)
        home = {c for c, n in counts.items() if n >= 0.2 * len(firm)}

        def resolve(value):
            key = self.resolve(value)
            if key:
                return key
            options = {k for k in self.ambiguous.get(_token(value), ())
                       if k.split("-")[0] in home}
            return next(iter(options)) if len(options) == 1 else None
        return resolve


def index(namespace: str, level: str, within=None) -> Index:
    """Lookup for the boundary units this namespace/level joins to."""
    if namespace == "NUTS":
        from mapsvc.cartography import gisco
        units = gisco.load(level, None, "simplified", within).units
        return _identity("NUTS", units)
    if namespace == "FIPS":
        from mapsvc.cartography import census
        units = census.load(level or "admin_2", None, "simplified", within).units
        return _identity("FIPS", units)
    from mapsvc.cartography import natural_earth
    sub = namespace == "ISO3166_2" or (namespace == "name" and level == "admin_1")
    if sub:
        units = natural_earth.load("admin_1", None, "simplified", within).units
        return _regions(units)
    units = natural_earth.load("admin_0", None, "simplified", within).units
    return _countries(units)


def _identity(namespace, units) -> Index:
    lookup, by_key = {}, {}
    for unit in units:
        key = (unit.get("keys") or {}).get(namespace)
        if not key:
            continue
        by_key[str(key)] = unit
        for token in (_token(key), "C:" + str(key).upper()):
            if token:
                lookup.setdefault(token, str(key))
    return Index(namespace, lookup, by_key)


def _countries(units) -> Index:
    lookup, by_key = {}, {}
    names: dict = {}
    for unit in units:
        key = (unit.get("keys") or {}).get("ISO3")
        if not key:
            continue
        by_key[key] = unit
        props = unit.get("properties") or {}
        for field in _COUNTRY_CODES + _COUNTRY_NUMBERS:
            token = _token(props.get(field))
            if token and not token.startswith("T:"):
                lookup.setdefault(token, key)
        lookup["C:" + key] = key
        for name in [unit.get("name")] + [props.get(f) for f in _COUNTRY_NAMES]:
            if name:
                names.setdefault("T:" + normalise_name(name), set()).add(key)
    # A name shared by two units (a sovereign and its territory) resolves to neither.
    lookup.update({t: next(iter(keys)) for t, keys in names.items()
                   if len(keys) == 1 and t not in lookup})
    return Index("ISO3", lookup, by_key)


def _regions(units) -> Index:
    lookup, by_key = {}, {}
    names: dict = {}
    short: dict = {}         # ISO suffix / postal code -> {keys}
    countries = {str(k).split("-")[0] for u in units
                 for k in [(u.get("keys") or {}).get("ISO3166_2")] if k}
    for unit in units:
        key = (unit.get("keys") or {}).get("ISO3166_2")
        if not key:
            continue
        by_key[key] = unit
        props = unit.get("properties") or {}
        lookup["C:" + key.upper()] = key
        hasc = _token(props.get("code_hasc"))
        if hasc:
            lookup.setdefault(hasc, key)
        # "SP" means São Paulo only inside Brazil: firm when the scope is one
        # country, otherwise settled by the rest of the file (Index.resolver).
        for code in {key.split("-", 1)[-1], props.get("postal")}:
            token = _token(code) if code else None
            if token and token.startswith("C:"):
                short.setdefault(token, set()).add(key)
        for name in [unit.get("name")] + [props.get(f) for f in _REGION_NAMES]:
            for variant in str(name or "").split("|"):
                if variant.strip():
                    names.setdefault("T:" + normalise_name(variant), set()).add(key)
    lookup.update({t: next(iter(keys)) for t, keys in names.items()
                   if len(keys) == 1 and t not in lookup})
    ambiguous = {t: keys for t, keys in names.items() if len(keys) > 1}
    for token, keys in short.items():
        if token in lookup:
            continue
        if len(countries) == 1 and len(keys) == 1:
            lookup[token] = next(iter(keys))
        else:
            ambiguous.setdefault(token, set()).update(keys)
    return Index("ISO3166_2", lookup, by_key, ambiguous)


def coverage(values, namespace: str, level: str, within=None) -> dict:
    """How many of the expected units the values resolve to.

    Subdivisions with no stated scope are measured against the countries the
    data itself lands in: a Brazil-by-state file is 27/27, not 27/4,000.
    """
    idx = index(namespace, level, within)
    values = list(values)
    resolve = idx.resolver(values)
    matched = {k for k in (resolve(v) for v in values) if k}
    if idx.namespace == "ISO3166_2" and not within:
        countries = {k.split("-")[0] for k in matched}
        expected = [k for k in idx.units if k.split("-")[0] in countries]
    else:
        expected = list(idx.units)
    return {"matched": len(matched), "expected": len(expected),
            "ratio": len(matched) / len(expected) if expected else 0.0,
            "scope": list(within) if within else None}
