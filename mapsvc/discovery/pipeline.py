"""From a request the registry could not answer to a validated, registered dataset.

    research agent -> probe -> validate -> register (or remember the failure)

Sources found earlier are not looked up here: they are already in the
registry the main agent searched (statistics source `discovered`, and the
`web` overlay search), so reaching this step means none of them fitted.

Every step that touches the network goes through mapsvc.discovery.http, so a
recorded run replays exactly. A failed search is remembered (negative cache)
so the same request does not pay for the same fruitless search twice.
"""

from __future__ import annotations

import dataclasses

from mapsvc.discovery import proposal as proposal_mod
from mapsvc.discovery import research as research_mod


@dataclasses.dataclass
class Outcome:
    record: dict | None          # the registered source, when one validated
    refusal: str | None          # why nothing was found, in words
    trace: list


def discover(prompt: str, reason: str, *, level: str | None = None, within=(),
             ask=None, search=None, fetch=None, probe_fn=None, store=None) -> Outcome:
    from mapsvc.discovery import probe as probe_mod

    if store is None:
        from mapsvc.discovery.store import Store
        store = Store()

    negative = store.recent_negative(prompt)
    if negative:
        return Outcome(None, "searched recently and nothing fit: "
                       + str((negative.get("report") or {}).get("reason", "")), [])

    if ask is None:
        from mapsvc.agent import ask_structured

        def ask(instructions, schema, conversation, name):
            return ask_structured(instructions, schema, conversation, name=name)
    if search is None or fetch is None:
        try:
            from mapsvc.discovery import providers
        except ImportError:
            return Outcome(None, "web search is not available in this build", [])
        search = search or providers.search
        fetch = fetch or providers.fetch
    probe_fn = probe_fn or probe_mod.probe

    need = research_mod.Need(prompt=prompt, reason=reason, level=level, within=tuple(within))
    result = research_mod.research(need, ask=ask, search=search, fetch=fetch, probe=probe_fn)
    if result.proposal is None:
        store.record_negative(prompt, {"reason": result.refusal, "trace": result.trace})
        return Outcome(None, result.refusal, result.trace)

    prop = result.proposal
    verdict = _verify(prop, probe_fn, within)
    result.trace.append({"step": "validate", "ok": verdict.ok, "problems": verdict.problems,
                         "coverage": verdict.coverage})
    if not verdict.ok:
        # One repair turn: the agent sees the service's reasons, as the main
        # agent does with validate(). A second failure is final.
        result = research_mod.repair(result, verdict.problems, ask=ask)
        if result.proposal is None:
            store.record_negative(prompt, {"reason": result.refusal, "trace": result.trace})
            return Outcome(None, result.refusal, result.trace)
        prop = result.proposal
        verdict = _verify(prop, probe_fn, within)
        result.trace.append({"step": "validate", "ok": verdict.ok, "problems": verdict.problems,
                             "coverage": verdict.coverage})
    if not verdict.ok:
        why = f"found {prop.url} but it failed validation: " + "; ".join(verdict.problems)
        store.record_negative(prompt, {"reason": why, "trace": result.trace})
        return Outcome(None, why, result.trace)

    record = store.add_or_update(proposal_mod.to_store_record(prop, verdict))
    return Outcome(record, None, result.trace)


def _verify(prop, probe_fn, within) -> proposal_mod.Verdict:
    """Re-read the proposed file and check the proposal against it."""
    from mapsvc.discovery import http, probe as probe_mod

    report = probe_fn(prop.url)
    from mapsvc.discovery import rowfilter
    rows, _fmt, _geo, _warn = probe_mod.records(http.get(prop.url, expect="data").content)
    rows = rowfilter.apply(rows, prop.row_filter)
    if not rows:
        return proposal_mod.Verdict(False, ["the filters keep no rows: "
                                            + rowfilter.describe(prop.row_filter)])
    if prop.kind == "choropleth":
        problem = _not_one_row_per_unit(rows, prop.roles)
        if problem:
            return proposal_mod.Verdict(False, [problem])
        # The vintage is what the data says, not what the agent read somewhere.
        if prop.roles.get("date"):
            years = [y for y in (_year(r.get(prop.roles["date"])) for r in rows) if y]
            if years:
                prop.vintage = str(max(years))

    def column_values(name):
        return [r.get(name) for r in rows]

    def coverage(values, namespace, level, scope):
        from mapsvc.discovery import join
        return join.coverage(values, namespace, level, scope or None)

    return proposal_mod.validate(prop, report, column_values=column_values,
                                 coverage=coverage, scope=list(within) or None)


def _year(raw):
    import re
    found = re.findall(r"(?<!\d)(1[89]\d\d|20\d\d)(?!\d)", str(raw or ""))
    return int(found[0]) if found else None


def _not_one_row_per_unit(rows, roles) -> str | None:
    """A long file must leave one row per unit and date after filtering.

    Measured: UNICEF's refugee SDMX table has two rows per country and year
    (AGE _T and Y0T17); without an AGE filter a map would show whichever came
    last. Say which columns still vary so the agent can add a filter.
    """
    unit = roles.get("key") or roles.get("name")
    date = roles.get("date")
    groups: dict = {}
    for r in rows:
        groups.setdefault((str(r.get(unit)), str(r.get(date)) if date else ""), []).append(r)
    dup = [g for g in groups.values() if len(g) > 1]
    if len(dup) <= max(2, 0.05 * len(groups)):
        return None
    skip = {unit, date, roles.get("value"), roles.get("name"), roles.get("key")}
    varying = []
    for column in dup[0][0]:
        if column in skip:
            continue
        seen = {str(r.get(column)) for g in dup[:50] for r in g}
        within = sum(len({str(r.get(column)) for r in g}) > 1 for g in dup[:50])
        if within >= 0.8 * min(50, len(dup)) and len(seen) <= 30:
            varying.append(f"{column} ({', '.join(sorted(seen)[:6])})")
    return (f"{len(dup)} of {len(groups)} unit/date pairs have several rows"
            + (f"; they differ in {'; '.join(varying[:3])} -- add a filter" if varying else ""))


def prompt_for_main_agent(record: dict) -> str:
    """How the main agent learns about a newly validated source."""
    if record.get("geometry_kind") == "choropleth":
        use = (f'use variable {{"source": "discovered", "id": "{record["id"]}"}} '
               f'at level {record.get("level")}')
    else:
        use = f'use an overlay {{"source": "web", "id": "{record["url"]}"}}'
    return ("The research step found and validated an open dataset for this request:\n"
            f"  {record.get('title')} -- {record.get('publisher')} ({record.get('vintage')}, "
            f"{record.get('licence')})\n"
            f"  kind {record.get('geometry_kind')}, keys {record.get('key_namespace')}\n"
            f"Reply with the full manifest and {use}. Choose the place and level as usual.")
