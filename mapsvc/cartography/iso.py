"""ISO3 <-> ISO2 translation.

Manifests address countries by ADM0_A3; Overture's `country` column is ISO2.
The table is built from the Natural Earth country file, which is already on
disk. That is a code lookup, not a geometry dependency -- no boundary from
Natural Earth reaches an Overture map.
"""

import functools

from mapsvc import registry


@functools.lru_cache(maxsize=1)
def _tables() -> tuple[dict, dict]:
    from mapsvc.harvest import load_source  # late: avoids an import cycle

    to_two: dict[str, str] = {}
    to_three: dict[str, str] = {}
    for feature in load_source("admin_0")["features"]:
        props = feature["properties"]
        # ISO_A2 is "-99" for eight countries, France and Norway among them;
        # the _EH variants carry the real code.
        two = props.get("ISO_A2_EH") or props.get("ISO_A2")
        if not two or two == "-99":
            continue
        two = two.upper()
        # Index every three-letter spelling Natural Earth offers. ADM0_A3 and
        # ISO_A3 disagree for disputed entities -- Kosovo is ADM0_A3 "KOS",
        # ISO_A3 "-99", ISO_A2_EH "XK" -- and callers may arrive with either.
        for key in ("ADM0_A3", "ISO_A3", "ISO_A3_EH"):
            three = props.get(key)
            if three and three != "-99":
                to_two.setdefault(three.upper(), two)
        # The reverse is one-to-many: ISO_A2_EH gives AU to Australia, the
        # Indian Ocean Territories (IOA) and Ashmore and Cartier (ATC). The
        # country that holds the code in its own ISO_A2 owns it; letting the
        # last row win mapped Overture's AU to ATC and hatched Australia.
        # The ISO3 spelling is the join key's (South Sudan is SSD, not NE's SDS).
        owns = str(props.get("ISO_A2") or "").upper() == two
        three = registry.iso3_of(props) or ""
        if three and (owns or two not in to_three):
            to_three[two] = three
    return to_two, to_three


def to_alpha2(code: str) -> str | None:
    """ISO3 (or ADM0_A3) to ISO2. Passes an ISO2 code straight through."""
    code = code.strip().upper()
    if len(code) == 2:
        return code
    return _tables()[0].get(code)


def to_alpha3(code: str) -> str | None:
    code = code.strip().upper()
    if len(code) == 3:
        return code
    return _tables()[1].get(code)


@functools.lru_cache(maxsize=1)
def _continents() -> dict[str, list[str]]:
    from mapsvc.harvest import load_source

    grouped: dict[str, list[str]] = {}
    for feature in load_source("admin_0")["features"]:
        props = feature["properties"]
        continent = str(props.get("CONTINENT", "")).lower()
        two = props.get("ISO_A2_EH") or props.get("ISO_A2")
        if continent and two and two != "-99":
            grouped.setdefault(continent, []).append(two.upper())
    return {k: sorted(set(v)) for k, v in grouped.items()}


def countries_in(continent: str) -> list[str]:
    """ISO2 codes for a CONTINENT name, empty if it is not one.

    Providers that filter by country -- Overture does -- need the membership
    list, because they carry no continent of their own.
    """
    return _continents().get(continent.strip().lower(), [])
