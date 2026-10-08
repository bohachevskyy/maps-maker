"""Derived variables: a quantity divided by another, or its change over time.

The manifest can say *what to show* beyond a raw column -- diesel consumption
per person, population change between 2000 and 2023 -- and this module turns
that into one set of keyed values, the same shape a source returns. Harvest
joins the result exactly as it would a raw variable.

Both derivations produce a ratio-level quantity: a per-capita figure or a
percent change has a meaningful zero (and a change can be negative), so a 0 is
an observation, not a placeholder. The measurement level travels in provenance.
"""

from mapsvc import statistics
from mapsvc.statistics import StatisticsError, Values

REGION = "world"

# What one unit of a denominator is called after "per".
_SINGULAR = {"people": "person", "persons": "person", "inhabitants": "inhabitant",
             "households": "household"}


def values(manifest) -> Values:
    """The variable as the manifest asks for it: raw, per something, or a change."""
    if manifest.change is None:
        return _at(manifest, None)
    start, end = manifest.change
    return _change(_at(manifest, start), _at(manifest, end), start, end)


def _at(manifest, year: int | None) -> Values:
    numerator = statistics.load(manifest.variable_source, manifest.variable_id,
                                manifest.level, REGION, year=year)
    if manifest.per is None:
        return numerator
    return _divide(numerator, manifest.per, manifest.level, year)


def _number(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value in (-99,):
        return None
    return float(value)


def _divide(numerator: Values, per: tuple, level: str, year: int | None) -> Values:
    """numerator / denominator, matching the denominator's year where it can.

    OWID's "latest" differs by country, so dividing every country by one
    vintage of the denominator would mix years. A time-series denominator is
    therefore loaded once per distinct numerator year and matched per key.
    """
    source, variable_id = per
    time_series = statistics.capabilities(source).time_series

    denominators: dict = {}

    def denominator_for(key) -> Values | None:
        wanted = year if year is not None else (
            numerator.years.get(key) if time_series else None)
        if wanted not in denominators:
            try:
                denominators[wanted] = statistics.load(source, variable_id, level,
                                                       REGION, year=wanted)
            except StatisticsError:
                if wanted is None:
                    raise
                # The denominator has no observation in that year at all.
                denominators[wanted] = None
        return denominators[wanted]

    def _latest() -> Values | None:
        if None not in denominators:
            denominators[None] = statistics.load(source, variable_id, level, REGION)
        return denominators[None]

    numerator_level = numerator.provenance.get("level")
    divisor_years: dict = {}
    out: dict = {}
    missing: dict = dict(numerator.missing)
    years: dict = {}
    for key in sorted(numerator.values):
        value = _number(numerator.values[key])
        if value is None or (numerator_level == "count" and value == 0):
            missing[key] = "no_data"
            continue
        den = denominator_for(key)
        divided_by = den
        divisor = None if den is None else _number(den.values.get(key))
        if not divisor and year is None and time_series and numerator.years.get(key) is not None:
            # No divisor from the numerator's own year -- area, for one, is
            # rarely dated. Outside a `change`, the divisor's latest value is
            # the honest fallback; the per-year spread in provenance shows it.
            latest = _latest()
            divided_by = latest
            divisor = _number(latest.values.get(key)) if latest else None
        if not divisor:
            missing[key] = "no_divisor"
            continue
        out[key] = value / divisor
        if key in numerator.years:
            years[key] = numerator.years[key]
        if divided_by is not None and divided_by.years.get(key) is not None:
            divisor_years[key] = divided_by.years[key]

    used = [d for d in denominators.values() if d is not None]
    if not used:
        raise StatisticsError(
            f"{source} has no {variable_id} values to divide by", "variable.per")
    den_prov = dict(used[0].provenance)
    if any(d.years for d in used):
        # A dated source: only the years of divisors actually used count. If
        # none of them was dated, there is no year to state.
        den_prov["year"] = None
    same_count_unit = _same_count_unit(numerator.provenance, den_prov)
    if same_count_unit:
        # A count divided by its count universe is a share, conventionally
        # displayed in percent. Returning 0.05 while labelling it a "rate" was
        # both surprising and incompatible with published ACS/BLS rates.
        out = {key: value * 100.0 for key, value in out.items()}
    return Values(values=out, missing=missing, years=years, divisor_years=divisor_years,
                  provenance=_per_provenance(numerator.provenance, den_prov,
                                             source, variable_id, divisor_years,
                                             same_count_unit))


def _same_count_unit(num: dict, den: dict) -> bool:
    """Two like counts form a percentage: people/people, households/households."""
    num_unit = str(num.get("unit") or "").strip().casefold()
    den_unit = str(den.get("unit") or "").strip().casefold()
    return (num.get("level") == den.get("level") == "count"
            and bool(num_unit) and num_unit == den_unit)


def _per_provenance(num: dict, den: dict, source: str, variable_id: str,
                    divisor_years: dict, same_count_unit: bool = False) -> dict:
    den_unit = den.get("unit")
    phrase = _SINGULAR.get(den_unit, den_unit) if den_unit else den.get("variable")

    unit = num.get("unit")
    if same_count_unit:
        unit = "%"
    elif unit and unit.endswith(" per year"):
        # "barrels per person per year", not "barrels per year per person".
        unit = f"{unit[:-len(' per year')]} per {phrase} per year"
    elif unit:
        unit = f"{unit} per {phrase}"
    else:
        unit = f"per {phrase}"
    out = dict(num)
    out.update({
        "variable": (f"{num.get('variable')} as a percentage of "
                     f"{_lower_first(den.get('variable'))}"
                     if same_count_unit else f"{num.get('variable')} per {phrase}"),
        "unit": unit,
        "level": "ratio",
        "per": {"source": source, "source_name": den.get("source"),
                "id": variable_id, "phrase": phrase,
                "variable": den.get("variable"), "unit": den_unit,
                "year": year_label(divisor_years.values()) or den.get("year"),
                "formula": ("numerator / denominator × 100 because both are "
                            f"counts of {den_unit}" if same_count_unit
                            else "numerator / denominator")},
        # Not every unit's divisor is dated; harvest restates this over the
        # units actually drawn.
    })
    if den.get("source") != num.get("source"):
        # Two sources, possibly two licences: both must be credited.
        out["attribution"] = f"{num.get('attribution')}; {den.get('attribution')}"
        if den.get("license") != num.get("license"):
            out["license"] = f"{num.get('license')}; {den.get('license')}"
    return out


def _lower_first(text) -> str:
    """'Population' -> 'population' for use mid-sentence; 'GDP' stays 'GDP'."""
    text = str(text or "")
    if len(text) > 1 and text[0].isupper() and text[1].islower():
        return text[0].lower() + text[1:]
    return text


def year_label(years) -> str | None:
    """'2022', or '2018–2022' for a spread; None when nothing is dated."""
    years = sorted(set(years))
    if not years:
        return None
    return str(years[0]) if len(years) == 1 else f"{years[0]}–{years[-1]}"


def _change(first: Values, last: Values, start: int, end: int) -> Values:
    """Percent change, (end - start) / start x 100, per key present in both."""
    out: dict = {}
    missing: dict = {**first.missing, **last.missing}
    for key in sorted(set(first.values) | set(last.values)):
        a = _number(first.values.get(key))
        b = _number(last.values.get(key))
        if a is None or b is None or a == 0:
            missing.setdefault(key, "no_data")
            continue
        out[key] = (b - a) / a * 100.0
        missing.pop(key, None)

    prov = dict(last.provenance)
    # A published national or statewide total, carried through as its own
    # percent change over the same years.
    aggregates = {}
    for prefix, end_total in (last.provenance.get("aggregates") or {}).items():
        start_total = (first.provenance.get("aggregates") or {}).get(prefix)
        a, b = _number(start_total and start_total.get("value")), _number(end_total.get("value"))
        if a and b is not None:
            aggregates[prefix] = {"label": end_total["label"], "value": (b - a) / a * 100.0}
    prov["aggregates"] = aggregates
    label = last.provenance.get("variable")
    prov.update({
        "variable": f"change in {_lower_first(label)}",
        "unit": f"% change {start}–{end}",
        "level": "ratio",
        "year": f"{start}–{end}",
        "change": {"from": start, "to": end,
                   "formula": f"({end} − {start}) / {start} × 100"},
    })
    return Values(values=out, missing=missing, provenance=prov)
