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
import unicodedata
from collections import Counter

_COUNTRY_CODES = ("ISO_A3", "ISO_A3_EH", "ADM0_A3", "ADM0_ISO", "WB_A3",
                  "ISO_A2", "ISO_A2_EH", "WB_A2")
_COUNTRY_NUMBERS = ("ISO_N3", "ISO_N3_EH", "UN_A3")
_COUNTRY_NAMES = ("NAME", "NAME_LONG", "FORMAL_EN", "NAME_EN", "NAME_SORT", "ADMIN",)
_REGION_PRIMARY = ("name", "woe_name", "gn_name")
_REGION_SECONDARY = ("name_en", "name_alt", "name_local")
# Units renamed since Natural Earth's edition (measured 2026-11-07: MX-DIF is
# still "Distrito Federal", and its name_en is "Mexico", which collides with
# México state). Names only -- never geometry.
_ALIASES = {"MX-DIF": ("Ciudad de México", "Mexico City", "CDMX")}


def normalise_name(value) -> str:
    """Casefolded, accents removed: "Ōsaka" -> "osaka", "São Paulo" -> "sao paulo".

    (Dropping non-ASCII letters instead turned "Ōsaka" into "saka".)
    """
    text = unicodedata.normalize("NFKD", str(value).casefold())
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


# Words that name the kind of unit, not the unit: "Tokyo-to", "Ōsaka-fu",
# "Kanagawa-ken", "Hokkaidō" vs "Hokkaido", "Lviv Oblast", "State of Mexico" is
# deliberately not here (it is a different unit from Mexico City).
_DESIGNATORS = {"to", "fu", "ken", "prefecture", "province", "provincia", "oblast",
                "region", "krai", "kraj", "county", "voivodeship", "department",
                "departamento", "governorate", "district", "estado", "state"}


_TOTALS = {"total", "totals", "sum", "national", "overall", "all", "average", "mean"}


def _bare(name_token: str) -> str | None:
    """'T:kanagawa ken' -> 'T:kanagawa'; None when nothing is stripped."""
    words = name_token[2:].split()
    kept = [w for w in words if w not in _DESIGNATORS]
    if kept and len(kept) < len(words):
        return "T:" + " ".join(kept)
    return None


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
    preferred: dict = dataclasses.field(default_factory=dict)   # token -> {keys by own name}

    def resolve(self, value) -> str | None:
        token = _token(value)
        found = self.lookup.get(token) if token else None
        if found is None and token and token.startswith("T:"):
            bare = _bare(token)
            found = self.lookup.get(bare) if bare else None
        if found is None:
            # Statistics offices often prefix names with their own codes:
            # "13_Tokyo-to", "01000 Hokkaido", "35 - São Paulo" (e-Stat, IBGE).
            m = re.match(r"^\s*\d+\s*[_\-–:.]?\s+(.+)$|^\s*\d+_(.+)$", str(value or ""))
            if m:
                found = self.lookup.get(_token(m.group(1) or m.group(2)))
        return found

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

        def settle(value):
            key = self.resolve(value)
            if key:
                return key
            token = _token(value)
            for t in (token, _bare(token) if token and token.startswith("T:") else None):
                options = {k for k in self.ambiguous.get(t, ()) if k.split("-")[0] in home}
                if len(options) > 1:
                    options &= self.preferred.get(t, set())
                if len(options) == 1:
                    return next(iter(options))
            return None

        def resolve(value):
            key = settle(value)
            if key or not isinstance(value, str):
                return key
            # "Mexico City (Ciudad de México)": either side of the brackets.
            m = re.match(r"^(.*?)\s*\((.+)\)\s*$", value)
            if m:
                key = settle(m.group(1)) or settle(m.group(2))
                if key:
                    return key
            # "Coahuila de Zaragoza", "Veracruz de Ignacio de la Llave": a unit's
            # name followed by its official suffix -- only inside this file's
            # country, and only when the shorter name is a whole unit name.
            words = normalise_name(value).split()
            if _TOTALS & set(words):
                return None          # "Mexico total" is the country, never México state
            for n in range(len(words) - 1, 0, -1):
                key = settle(" ".join(words[:n]))
                if key and key.split("-")[0] in home:
                    return key
            return None
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
    primary_names: dict = {}
    preferred: dict = {}
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
        primary = [unit.get("name")] + [props.get(f) for f in _REGION_PRIMARY]
        primary += list(_ALIASES.get(key, ()))
        for tier, group in ((primary_names, primary),
                            (names, [props.get(f) for f in _REGION_SECONDARY])):
            for name in group:
                for variant in str(name or "").split("|"):
                    if variant.strip():
                        tier.setdefault("T:" + normalise_name(variant), set()).add(key)
    # Firm only when every unit carrying the words is the same one. Otherwise
    # the file's country settles it, and inside one country a unit's own name
    # beats another's translation (NE's MX-DIF has name_en "Mexico").
    preferred.update({t: keys for t, keys in primary_names.items()})
    for t, keys in primary_names.items():
        names.setdefault(t, set()).update(keys)
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
    return Index("ISO3166_2", lookup, by_key, ambiguous, preferred)


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
        # Unnamed slivers (Natural Earth's MX-X01~) are not units anyone reports.
        expected = [k for k, u in idx.units.items()
                    if k.split("-")[0] in countries and u.get("name")]
    else:
        expected = list(idx.units)
    return {"matched": len(matched), "expected": len(expected),
            "ratio": len(matched) / len(expected) if expected else 0.0,
            "scope": list(within) if within else None}
