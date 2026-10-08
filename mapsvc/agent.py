"""Turning a customer's sentence into a manifest.

The agent's only job is assembly. It does not render, and it is not trusted:
whatever it produces goes through `manifest.validate` exactly like a
hand-written manifest, and a rejection is fed back for one repair attempt.

The schema is generated from the registered sources, so a fixed source's ids
cannot be invented and a searchable source's must be one the search returned.
What the schema cannot express -- that a bbox's corners must be the right way
round, that a variable belongs to one level -- is why the validator stays in the
loop, with one repair attempt when it objects.
"""

import functools
import json
import os
import pathlib
import shutil
import subprocess
import tempfile

from mapsvc import overlays, registry, statistics
from mapsvc.manifest import Manifest, ManifestError, validate
from mapsvc.statistics import StatisticsError

DEFAULT_MODEL = "gpt-5.6-terra"
MAX_OUTPUT_TOKENS = 2000
REPAIR_ATTEMPTS = 1
SEARCH_RESULTS = 8
CODEX_TIMEOUT = 180


class AgentError(ValueError):
    """The prompt could not be turned into a valid manifest."""

    def __init__(self, message: str, field: str):
        super().__init__(message)
        self.field = field


class AgentRefusal(AgentError):
    """The model understood the prompt but no available source can answer it."""


class AgentUnavailable(RuntimeError):
    """The agent is not configured -- a deployment problem, not a bad request."""


def model_name() -> str:
    return os.environ.get("OPENAI_MODEL", DEFAULT_MODEL)


def backend_name() -> str:
    """Choose the configured LLM backend without importing either client."""
    configured = os.environ.get("MAPSVC_LLM")
    if configured:
        backend = configured.strip().lower()
        if backend not in {"openai", "codex"}:
            raise AgentUnavailable(
                "MAPSVC_LLM must be 'openai' or 'codex', got "
                f"{configured!r}."
            )
        return backend
    if not os.environ.get("OPENAI_API_KEY") and shutil.which("codex"):
        return "codex"
    return "openai"


def _client():
    if not os.environ.get("OPENAI_API_KEY"):
        raise AgentUnavailable(
            "OPENAI_API_KEY is not set; POST /describe needs it. POST /map "
            "works without it."
        )
    from openai import OpenAI  # imported lazily so /map never needs the SDK

    return OpenAI()


@functools.lru_cache(maxsize=1)
def schema() -> dict:
    """The response schema, generated from the registry."""
    manifest_properties = {
        "bbox": {
            "type": "array",
            "description": "the map window as [min_lon, min_lat, max_lon, max_lat] "
                           "in degrees. min_lon must be west of max_lon and "
                           "min_lat south of max_lat.",
            "items": {"type": "number"},
        },
        "within": {
            "type": "array",
            "description": "country codes (ISO3) and/or ISO 3166-2 unit codes "
                           "to scope the map to. Empty for a bbox-only window.",
            "items": {"type": "string"},
        },
        "level": {"type": "string", "enum": list(registry.LEVELS)},
        "basemap": {
            "type": "object",
            "properties": {
                "source": {"type": "string", "enum": list(registry.BASEMAPS)},
                "detail": {"type": "string", "enum": list(registry.DETAILS)},
            },
            "required": ["source", "detail"],
            "additionalProperties": False,
        },
        "variable": {
            "type": "object",
            "properties": {
                "source": {"type": "string", "enum": list(statistics.sources())},
                # Deliberately not an enum. A searched id cannot be enumerated
                # ahead of time, and an enum here silently forbids the very
                # answers the search exists to find. The constraint is enforced
                # instead: a fixed source's id must be in its catalogue (the
                # validator), and a searchable source's must be one the search
                # actually returned (checked below). That is a real check rather
                # than a hope, so nothing is lost by dropping the enum.
                "id": {"type": "string",
                       "description": "for a fixed source, one of its listed ids; "
                                      "for a searchable one, empty on the first "
                                      "turn then exactly one returned candidate"},
                "search_query": {
                    "type": "string",
                    "description": "for a searchable source: the measure to look "
                                   "for, e.g. 'unemployment rate'. Empty otherwise.",
                },
                # Strict mode wants every property present, so "absent" is an
                # empty source here and 0/0 for change; describe() strips them.
                "per": {
                    "type": "object",
                    "description": "divide the variable by another, e.g. per "
                                   "person. Empty source and id for none.",
                    "properties": {
                        "source": {"type": "string",
                                   "enum": [""] + list(statistics.sources())},
                        "id": {"type": "string"},
                    },
                    "required": ["source", "id"],
                    "additionalProperties": False,
                },
                "change": {
                    "type": "object",
                    "description": "percent change of the variable from one year "
                                   "to another. 0 and 0 for none.",
                    "properties": {
                        "from": {"type": "integer"},
                        "to": {"type": "integer"},
                    },
                    "required": ["from", "to"],
                    "additionalProperties": False,
                },
            },
            "required": ["source", "id", "search_query", "per", "change"],
            "additionalProperties": False,
        },
        "overlays": {
            "type": "array",
            "description": "0 to 3 feature layers drawn above the basemap: "
                           "points, lines or flows from an overlay source. "
                           "Empty for none.",
            "items": {
                "type": "object",
                "properties": {
                    "source": {"type": "string", "enum": list(overlays.sources())},
                    "id": {"type": "string",
                           "description": "in the source's own grammar; for a "
                                          "searchable source, empty on the first "
                                          "turn or an https URL"},
                    "search_query": {"type": "string",
                                     "description": "for a searchable overlay "
                                                    "source: what dataset to look "
                                                    "for. Empty otherwise."},
                },
                "required": ["source", "id", "search_query"],
                "additionalProperties": False,
            },
        },
        # null means a base map: boundaries with nothing painted on them.
        "variable_is_null": {
            "type": "boolean",
            "description": "true for a base map -- draw the boundaries, shade nothing",
        },
    }
    return {
        "type": "object",
        "properties": {
            "mappable": {
                "type": "boolean",
                "description": "false if no registry variable answers the request",
            },
            "refusal": {
                "type": ["string", "null"],
                "description": "when mappable is false, why, naming what is available",
            },
            "reasoning": {
                "type": "string",
                "description": "one or two sentences on the choices made",
            },
            # Always present, even when refusing: a nullable object would need
            # anyOf, and keeping the schema to the plainest subset of strict
            # mode is worth one discarded object.
            "manifest": {
                "type": "object",
                "properties": manifest_properties,
                "required": list(manifest_properties),
                "additionalProperties": False,
            },
        },
        "required": ["mappable", "refusal", "reasoning", "manifest"],
        "additionalProperties": False,
    }


def _describe(variables: dict) -> str:
    return "\n".join(
        f"  {name}: "
        + (f"{meta.label} -- " if meta.label and meta.label != name else "")
        + f"{meta.level}"
        + (f", in {meta.unit}" if meta.unit else "")
        + (f", vintage from {meta.year_col}" if meta.year_col else "")
        for name, meta in variables.items()
    )


def instructions() -> str:
    variables = _describe(statistics.variables_for("admin_0", "natural_earth"))
    admin1 = _describe(statistics.variables_for("admin_1", "natural_earth"))
    blocks = []
    for c in statistics.all_capabilities():
        kind = "SEARCHABLE" if c.searchable else "FIXED"
        keys = ", ".join(f"{level} by {c.key_at(level)}" for level in c.levels)
        block = (f"  source {c.source!r} -- {kind}"
                 f"{', TIME SERIES' if c.time_series else ''}; keyed {keys}."
                 f"\n    {c.description}")
        if c.denominators:
            block += "\n    divisors usable in variable.per without searching:"
            for divisor, meaning in c.denominators.items():
                block += f"\n      {divisor}: {meaning}"
        if not c.searchable:
            # List a fixed source's whole catalogue, whichever source it is --
            # naming natural_earth here would leave a newly registered fixed
            # source invisible to the agent.
            # A catalogue shared by several levels is listed once.
            listings: dict[str, list[str]] = {}
            for level in c.levels:
                listed = _describe(statistics.variables_for(level, c.source))
                if listed:
                    listings.setdefault(listed, []).append(level)
            for listed, levels in listings.items():
                block += f"\n    at {', '.join(levels)}:\n{listed}"
        blocks.append(block)
    sources = "\n\n".join(blocks)
    from mapsvc import cartography

    basemap_keys = "\n".join(
        f"  {name:<14}" + "; ".join(
            f"{level} carries {', '.join(cartography.keys(name, level)) or 'no key'}"
            for level in cartography.levels(name))
        for name in registry.BASEMAPS)
    max_units = f"{registry.MAX_UNITS:,}"
    region_hint = ("Country codes in `within` are ISO3, except where a basemap "
                   "says otherwise above (census takes US-XX states, gisco "
                   "takes EU and NUTS codes).")

    return f"""You assemble manifests for a map service. You choose what to show \
and where; you never choose how it is drawn. Colour, classification, projection \
and missing-value handling are all derived from the data, so there are no fields \
for them and you cannot get them wrong.

A manifest has four things: a window (bbox), a granularity (level), where the \
outlines come from (basemap), and optionally what to shade them by (variable). \
The variable can be divided by another (variable.per) or turned into a percent \
change between two years (variable.change).

WHAT TO SHOW: `within`, `bbox`, or both

`within` is a political filter: a list of country codes (ISO3) and/or ISO 3166-2 \
unit codes. Only units belonging to those appear. Prefer it whenever the request \
names real places, because it gives exact borders and no neighbours.

  "Ukraine's oblasts"      within ["UKR"],  level admin_1   -> 27 units
  "the raions of Volyn"    within ["UA-07"], level admin_2  -> exactly 4
  "the Balkans"            within ["ALB","BIH","BGR","HRV","GRC","KOS","MNE",
                                   "MKD","ROU","SRB","SVN"]
  "Scandinavia"            within ["NOR","SWE","DNK"] (add FIN, ISL if the
                           request means the Nordics rather than Scandinavia)
  "Europe"                 within ["AUT","BEL","BGR","HRV","CYP","CZE","DNK",
                                   "EST","FIN","FRA","DEU","GRC","HUN","IRL",
                                   "ITA","LVA","LTU","LUX","MLT","NLD","POL",
                                   "PRT","ROU","SVK","SVN","ESP","SWE","ISL",
                                   "LIE","NOR","CHE","GBR","ALB","BIH","KOS",
                                   "MNE","MKD","SRB","UKR","MDA","BLR"]

An informal region has no code, so you supply the member list yourself. Say in \
reasoning which countries you counted, since reasonable people draw the Balkans \
and Scandinavia differently. For Europe, use the list above: EU27 + EFTA + the \
UK + the Western Balkans + Ukraine, Moldova and Belarus. Exclude Russia, Turkey \
and the Caucasus unless the user asks for them.

Named political regions use `within` only. Do not also put a bounding box around \
Europe, the EU, the Balkans, or another named region: a box admits unwanted \
neighbours and can clip legitimate islands and outermost regions. The renderer \
derives the extent and insets from the selected units. In particular, "EU \
regions" is within ["EU"] with no bbox, so Martinique, Reunion, the Canaries \
and the Azores remain eligible for insets.

`bbox` is [min_lon, min_lat, max_lon, max_lat] in degrees -- a spatial window. \
Everything overlapping it is returned, neighbours included. Use it when the \
request is about an *area* rather than named units: "around Kyiv", "the Alps", \
"the area within 100km of Lviv", or when zooming to a place that is not an \
administrative unit at all.

  around Kyiv     [29.2, 49.2, 32.2, 51.6]
  the whole world [-180, -90, 180, 90]

Round bbox to about a tenth of a degree; precision beyond that is false. A \
window crossing the antimeridian is not supported -- for the Pacific, pick one \
side.

Both together narrows twice: within ["UKR"] plus a bbox around Lviv gives \
Ukrainian units in the west only, with nothing Polish. Give at least one.

Country codes are ISO3 as Natural Earth spells them: Kosovo is KOS or XK, not \
XKX.

THE GRANULARITY

Units are returned if they *overlap* the window, so a box around Ukraine also \
returns Polish voivodeships and Romanian counties. That is what a map of an \
area looks like; do not try to avoid it.

Match the level to the size of the window, or the map is unreadable and the \
request is refused for carrying too many units. Rough guide, measured:

  window       admin_0    admin_1    admin_2    admin_3
  70x38 deg         58      1,281     11,497    304,530     <- continent
  19x9 deg          11        139      3,434     47,538     <- one country
  5x4 deg            7         34        520      9,779     <- a few countries
  3x2 deg            4         10         25      1,303     <- one province

Aim for roughly 20 to 400 units. So: a continent means admin_0, one country \
means admin_1, a province or a metro area means admin_2. admin_3 is only ever \
right for a very small window. More than {max_units} units is rejected.

Two more geographies have their own levels and basemaps, measured:

  EU (within ["EU"])   nuts_1: 92    nuts_2: 244    nuts_3: 1,165 (too many)
  Germany (within ["DEU"])                         nuts_3: 400
  US counties          California 58, Texas 254, all of the US 3,222 (too many:
                       scope admin_2 to one or a few states, e.g. ["US-CA"])

{sources}

FIXED sources are small and fully listed above. Put the id straight into \
variable.id and leave search_query empty.

SEARCHABLE sources are far too large to list. Do NOT guess an id for one. \
Leave variable.id empty and put a plain-English phrase naming the measure into \
variable.search_query -- "unemployment rate", "deaths from air pollution". The \
service will search and come back with real candidates; you then pick one of \
those ids exactly. Choosing an id that was not offered will fail.

Write the query as the measure itself, not as the user's sentence. "How many \
people are out of work in Europe?" should search for "unemployment rate".

THE BASEMAP

  overture      -- the default. admin_0 through admin_3, far the most detail. \
Queried live, so a new window takes about ten seconds; afterwards it is cached.
  natural_earth -- admin_0 and admin_1 only, much coarser, but instant. Choose \
it when the user asks for something fast or rough, or for a window the size of \
a continent, where Overture is slow enough to be a problem. Its admin_1 is \
incomplete in disputed areas -- Ukraine has 25 units there, without Crimea and \
Sevastopol -- so for the sub-national units of one country use overture (or \
census for the US), which carries every unit.
  census        -- US Census Bureau: US states (admin_1) and counties (admin_2). \
The only basemap whose counties carry FIPS codes. within: "USA" or states as \
"US-CA".
  gisco         -- Eurostat GISCO: NUTS regions (nuts_1, nuts_2, nuts_3), the \
only basemap with NUTS codes. within: "EU", countries (ISO3), or NUTS codes.

JOIN KEYS: a variable can only be shaded onto boundaries that carry its key. \
Each source above says how it is keyed; each basemap carries:

{basemap_keys}

So: US county statistics need basemap census at admin_2; Eurostat needs gisco \
at a nuts level; ISO 3166-2 statistics (Ukrstat, EIA states) work on overture, \
natural_earth or census admin_1. Overture's admin_2 and admin_3 carry no key, \
so they are boundaries only. A mismatch is rejected.

Set variable_is_null to true for a base map -- boundaries with no shading. \
That is right whenever someone asks to *see* or *draw* units rather than compare \
a quantity.

DERIVED VARIABLES: variable.per and variable.change

variable.per divides the variable by another one, keyed the same way: per \
person, per km², a rate out of its universe. Use it when the request says "per \
capita", "per person", "per inhabitant", "density" or "rate" and the source \
has no ready-made ratio. Also infer normalization when comparing totals across \
units of substantially unequal size: consumption, emissions, GDP and event or \
people counts should normally be per capita; spatial measures whose opportunity \
scales with land should normally be per km². Use raw totals only when the user \
explicitly asks for totals. Population itself is the exception: map it as a \
count, because the renderer automatically uses proportional circles for counts. \
State the inferred denominator in reasoning, for example "normalized per capita \
because state populations differ greatly."

Prefer a ready-made ratio published by a source over deriving the same ratio. \
For US county unemployment, prefer BLS LAUS's published annual-average \
unemployment_rate over deriving the ACS survey fraction. \
When the request itself names a ratio -- "GDP per capita", "share of ...", a \
rate -- search a searchable source for that exact measure first and use what it \
publishes; derive with `per` only when no source publishes it. Natural Earth's \
GDP_MD is a nominal, single-year estimate: never use GDP_MD / POP_EST when OWID \
offers GDP per capita. \
For GDP comparisons across countries or regions, prefer purchasing-power \
standards per inhabitant over nominal currency: for Eurostat regional GDP choose \
unit=PPS_EU27_2020_HAB, not unit=EUR_HAB. This makes living-standard comparisons \
meaningful across different price levels.

When a derived ratio is needed, the divisor must be a fixed source's listed id, \
or one of the divisors a searchable source lists above -- never a guessed id. \
Leave per.source and per.id empty otherwise.

  "diesel consumption across US states" (infer per capita; say so in reasoning)
      variable {{source eia, id <the distillate series found by search>,
                per {{source eia, id TPOPP}}}}
  "poverty rate by California county"
      variable {{source census_acs, id B17001_002E,
                per {{source census_acs, id B17001_001E}}}}

variable.change maps the percent change between two years, (to - from) / from \
x 100, from a TIME SERIES source. Use it whenever the request asks how much \
something changed, grew or shrank between years: search for the measure \
itself, never for a "change in ..." chart. Leave both at 0 otherwise.

  "population change 2000-2023"   search "population", change {{from 2000, to 2023}}

{_overlay_section()}

REFUSING

Refusal is about what is being measured, never about where or how. Set mappable \
to false only when no source -- statistics or overlay -- publishes the requested \
data, or when a search comes back empty. Say so plainly and name what does \
exist. Earthquakes, railways, roads, rivers, power lines and historical routes \
are mappable as overlays; do not refuse them. Never refuse because a place has \
no official code: name its members in `within`, or draw a `bbox` around it.

{region_hint}"""


def _overlay_section() -> str:
    blocks = []
    for c in overlays.all_capabilities():
        kind = "SEARCHABLE" if c.searchable else "FIXED GRAMMAR"
        blocks.append(f"  source {c.source!r} -- {kind}; licence {c.license}.\n"
                      f"    {c.description}.\n    id: {c.id_help}.")
    listed = "\n".join(blocks)
    return f"""OVERLAYS: points, lines and flows above the base map

Statistics shade polygons. Some requests are about things that are not \
polygons at all: earthquakes, railway lines, an army's route. Those go in \
`overlays`, a list of up to three {{source, id}} layers drawn over a muted base \
map. How a layer is drawn -- circle sizes, line colours, band widths -- is \
derived from its data; you only say what to fetch. A map can have overlays \
and no variable (variable_is_null true), which is the usual case.

{listed}

The window still comes from bbox/within, and the base map still needs a level \
and basemap: pick a coarse, fast one (natural_earth; admin_0, or admin_1 for \
one country) so the overlay carries the message.

  "strong earthquakes around Japan since 2011"
      bbox [128.0, 29.0, 147.0, 46.0], level admin_1, basemap natural_earth,
      variable_is_null true,
      overlays [{{source usgs, id "start=2011-01-01;end=2026-01-01;minmagnitude=6"}}]
  "Germany's motorway network"
      within ["DEU"], level admin_1, basemap natural_earth, variable_is_null
      true, overlays [{{source overpass, id "highway=motorway"}}]
  "Atlantic hurricane tracks"
      no bbox, no within, level admin_0, basemap natural_earth,
      variable_is_null true, overlays [{{source web, id "", search_query
      "atlantic hurricane storm tracks"}}] -- then pick from the candidates.

You cannot see a web dataset's extent before it is fetched, so for web \
overlays leave bbox and within empty: the map is framed on the data itself. \
usgs and overpass need a window from you (overpass refuses without one).

A `within` country includes its overseas parts (France's Caribbean and Indian \
Ocean departments); with an overlay the map frames itself on the units near \
the overlay, so `within` alone is right for a country's network. Add a bbox \
only to zoom in. A world map is bbox [-180, -90, 180, 90]. An \
end date is exclusive: all of 2019 is start=2019-01-01;end=2020-01-01.

Reliability rules are enforced after your answer: a bbox combined with `within` \
must retain every named political unit; admin_0 windows wider than 90 degrees \
are automatically switched to Natural Earth; and Overpass tags must be real, \
specific OSM key=value pairs (never `*`) that match at least one feature. A \
rejection will name clipped units or suggest common tag values, so repair the \
manifest by dropping/widening the bbox or correcting/narrowing the tag.

For `web`, never invent data and never paste values: either search the curated \
catalogue (empty id, a search_query) and pick returned URLs, or give an https \
URL of a CSV/GeoJSON you are confident exists. The service fetches it and \
rejects it if it does not load or has no coordinates. Leave search_query empty \
for usgs and overpass."""


def describe(prompt: str) -> tuple[Manifest, dict, str]:
    """Assemble and validate a manifest from a natural-language request.

    Two turns when a searchable source is chosen: the first names the source and
    a query, the service runs the search, and the second picks from the
    candidates it returns. The agent can therefore reach an indicator this code
    has never heard of, but cannot invent one -- the choice is constrained to
    real search hits rather than to a frozen list.

    Returns the validated manifest, its raw dict form, and the agent's reasoning.
    """
    if not isinstance(prompt, str) or not prompt.strip():
        raise AgentError("prompt must be a non-empty string", "prompt")

    conversation: list[dict] = [{"role": "user", "content": prompt.strip()}]
    last_error: ManifestError | None = None
    offered: set[str] | None = None
    overlays_searched = False
    repairs = 0

    for attempt in range(REPAIR_ATTEMPTS + 2):
        answer = _ask(conversation)

        if not answer.get("mappable", False):
            raise AgentRefusal(
                answer.get("refusal") or "this request cannot be mapped from the "
                "available data", "prompt"
            )

        raw = dict(answer["manifest"])
        query = (raw.get("variable") or {}).get("search_query") or ""
        chosen = (raw.get("variable") or {}).get("id") or ""
        source = (raw.get("variable") or {}).get("source")

        wants_variable = bool(source and not chosen and query and offered is None)
        overlay_queries = [] if overlays_searched else _overlay_queries(raw)
        if wants_variable or overlay_queries:
            parts = []
            if wants_variable:
                found = _search(source, query)
                offered = {c.id for c in found}
                parts.append(_candidate_prompt(source, query, found))
            for overlay_source, overlay_query in overlay_queries:
                parts.append(_overlay_candidates(overlay_source, overlay_query))
            overlays_searched = overlays_searched or bool(overlay_queries)
            conversation.append({"role": "assistant", "content": json.dumps(answer)})
            conversation.append({"role": "user", "content": "\n\n".join(parts)})
            continue

        if offered is not None and chosen not in offered:
            # The id has to be one the search returned. Without this the model
            # could invent a plausible-looking slug, which would 404 later with
            # a far less useful message -- or worse, silently resolve to the
            # wrong chart.
            raise AgentError(
                f"{chosen!r} was not among the {len(offered)} candidates "
                f"{source} returned for {query or 'that search'}", "variable.id",
            )
        # Structured Outputs cannot make one field nullable-by-flag, so the
        # model signals a base map with a boolean and we clear the object here.
        raw.get("variable", {}).pop("search_query", None)
        _strip_derivations(raw.get("variable") or {})
        # An empty list means "no political filter"; the validator wants absence.
        if not raw.get("within"):
            raw.pop("within", None)
        if not raw.get("bbox"):
            raw.pop("bbox", None)
        if raw.pop("variable_is_null", False):
            raw["variable"] = None
        _strip_overlays(raw)
        try:
            _check_per((raw.get("variable") or {}).get("per"))
            manifest = validate(raw)
            # validate() can apply derived source rules. Return the effective
            # manifest rather than claiming the model's rejected source was used.
            if ("basemap" in raw
                    and raw.get("basemap", {}).get("source") != manifest.basemap_source):
                raw["basemap"]["source"] = manifest.basemap_source
            _preflight(manifest)
            return manifest, raw, answer.get("reasoning", "")
        except ManifestError as error:
            last_error = error
            # Counted apart from `attempt`: the search turn is not a repair,
            # and letting it use up the budget meant a searchable source's
            # manifest was never repaired at all.
            if repairs == REPAIR_ATTEMPTS:
                break
            repairs += 1
            # Hand the rejection back rather than guessing at a fix here; the
            # validator's message already names the field and the reason.
            conversation.append({"role": "assistant", "content": json.dumps(answer)})
            conversation.append({
                "role": "user",
                "content": f"That manifest was rejected: {error} "
                           f"(field: {error.field}). Return a corrected manifest.",
            })

    raise AgentError(
        f"could not assemble a valid manifest after {REPAIR_ATTEMPTS + 1} attempts: "
        f"{last_error}", getattr(last_error, "field", "prompt")
    )



def _overlay_queries(raw: dict) -> list[tuple[str, str]]:
    """(source, query) for every searchable overlay the model left unchosen."""
    out = []
    for item in raw.get("overlays") or []:
        source = item.get("source")
        if (source in overlays.sources() and overlays.capabilities(source).searchable
                and not item.get("id") and item.get("search_query")):
            out.append((source, item["search_query"]))
    return out


def _overlay_candidates(source: str, query: str) -> str:
    try:
        found = overlays.search(source, query, limit=SEARCH_RESULTS)
    except overlays.OverlayError:
        found = []
    if not found:
        return (f"The {source} overlay catalogue has nothing for {query!r}. Give an "
                "https URL of a CSV or GeoJSON you are confident exists, or refuse.")
    listed = "\n".join(f"  {c.as_prompt_line()}" for c in found)
    return (f"The {source} overlay catalogue returned these datasets for {query!r}:\n"
            f"{listed}\n\nReply with the full manifest, one overlay per file you "
            "want (source web, id = the URL exactly, search_query empty). Use "
            "several when they belong together, e.g. a route and its towns.")


def _strip_overlays(raw: dict) -> None:
    """Strict mode's always-present search_query goes; an empty list goes too."""
    items = raw.get("overlays")
    if not items:
        raw.pop("overlays", None)
        return
    raw["overlays"] = [{"source": item.get("source"), "id": item.get("id")}
                       for item in items]


def _preflight(manifest: Manifest) -> None:
    """Run checks whose failures need to reach the model's repair turn.

    Web and Overpass bytes are cached, so the subsequent harvest does not fetch
    them twice. Political bbox coverage is checked here as well as at harvest:
    direct manifests still get the invariant, while agent manifests can repair
    it by dropping or widening the bbox.
    """
    from mapsvc import harvest
    from mapsvc.overlays import overpass, web

    try:
        harvest.validate_window(manifest)
    except harvest.HarvestError as exc:
        raise ManifestError(str(exc), exc.field) from None

    for i, (source, overlay_id) in enumerate(manifest.overlays):
        try:
            if source == "web":
                web.check(overlay_id)
            elif source == "overpass":
                # Only the result can prove the filter matched zero or far too
                # many features; load() caches it for the following harvest.
                window = overlays.Window(manifest.bbox, manifest.within,
                                         manifest.bbox or (-180, -90, 180, 90))
                overpass.load(overlay_id, window)
        except overlays.OverlayError as exc:
            raise ManifestError(str(exc), f"overlays[{i}].id") from None


def _strip_derivations(variable: dict) -> None:
    """Turn strict mode's "empty means absent" back into absence."""
    per = variable.get("per")
    if not per or not per.get("source") or not per.get("id"):
        variable.pop("per", None)
    change = variable.get("change")
    if not change or not change.get("from") or not change.get("to"):
        variable.pop("change", None)


def _check_per(per: dict | None) -> None:
    """A divisor from a searchable source must be one it declares up front.

    The agent may not invent a searchable id, and it is shown no search for the
    divisor -- only the source's declared denominators, which are therefore
    the whole of what it can choose.
    """
    if not per or per.get("source") not in statistics.sources():
        return  # absent, or the validator will name the unknown source
    capabilities = statistics.capabilities(per["source"])
    if capabilities.searchable and per.get("id") not in capabilities.denominators:
        offered = ", ".join(capabilities.denominators) or "none"
        raise ManifestError(
            f"variable.per {per['source']}/{per.get('id')!r} was not offered; "
            f"{per['source']} offers these divisors: {offered}", "variable.per.id")


def _search(source: str, query: str) -> list:
    try:
        return statistics.search(source, query, limit=SEARCH_RESULTS)
    except StatisticsError:
        return []


def _candidate_prompt(source: str, query: str, found: list) -> str:
    """Hand the search results back for the next turn."""
    if not found:
        return (f"Searching {source} for {query!r} returned nothing. "
                f"{source} does not publish this. Refuse, unless another source "
                "can answer it.")
    listed = "\n".join(f"  {c.as_prompt_line()}" for c in found)
    return (
        f"{source} returned these candidates for {query!r}:\n{listed}\n\n"
        "Reply with the full manifest, setting variable.id to exactly one of "
        "those ids and leaving search_query empty. Prefer the candidate that "
        "matches the request most directly and has the widest country coverage. "
        "Apply the unit preferences in the main instructions: in particular, "
        "cross-country or regional GDP uses purchasing-power standards per "
        "inhabitant (PPS), not nominal euros. "
        "Avoid ones that split by sex, age or subcategory unless asked. If none "
        "of them is what was asked for, refuse instead of settling."
    )


def _ask(conversation: list[dict]) -> dict:
    backend = backend_name()
    if backend == "codex":
        return _ask_codex(conversation)
    return _ask_openai(_client(), conversation)


def _ask_openai(client, conversation: list[dict]) -> dict:
    response = client.responses.create(
        model=model_name(),
        instructions=instructions(),
        input=conversation,
        max_output_tokens=MAX_OUTPUT_TOKENS,
        text={"format": {"type": "json_schema", "name": "map_request",
                         "strict": True, "schema": schema()}},
    )
    try:
        return _first_json_object(response.output_text)
    except (json.JSONDecodeError, AttributeError) as exc:
        raise AgentError(f"the model did not return usable JSON: {exc}", "prompt") from exc


def _ask_codex(conversation: list[dict]) -> dict:
    executable = shutil.which("codex")
    if executable is None:
        raise AgentUnavailable(
            "the codex CLI is not on PATH; install it or set MAPSVC_LLM=openai "
            "and OPENAI_API_KEY. POST /map works without either backend."
        )

    transcript = instructions() + "\n\n" + "\n".join(
        f"{turn['role'].upper()}: {turn['content']}" for turn in conversation
    )
    with tempfile.TemporaryDirectory(prefix="mapsvc-codex-") as directory:
        root = pathlib.Path(directory)
        schema_path = root / "schema.json"
        output_path = root / "output.json"
        schema_path.write_text(json.dumps(schema(), sort_keys=True), encoding="utf-8")
        command = [
            executable, "exec", "--ephemeral", "--skip-git-repo-check",
            "-s", "read-only", "-c", "model_reasoning_effort=low",
        ]
        command.extend(["-m", os.environ.get("MAPSVC_CODEX_MODEL", DEFAULT_MODEL)])
        command.extend([
            "--output-schema", str(schema_path), "-o", str(output_path), "-",
        ])
        try:
            completed = subprocess.run(
                command, input=transcript, text=True, capture_output=True,
                timeout=CODEX_TIMEOUT, check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise AgentError(
                f"the codex CLI timed out after {CODEX_TIMEOUT}s", "prompt"
            ) from exc
        except OSError as exc:
            raise AgentUnavailable(f"the codex CLI could not be started: {exc}") from exc

        if completed.returncode:
            detail = (completed.stderr or completed.stdout or "no diagnostic").strip()
            # The CLI prints a banner and the whole prompt before failing, so
            # the first 500 characters never contain the reason. Its ERROR
            # lines do (a usage limit, an auth failure); fall back to the tail.
            errors = [line.strip() for line in detail.splitlines()
                      if line.strip().startswith("ERROR")]
            reason = "; ".join(dict.fromkeys(errors)) if errors else detail[-500:]
            raise AgentError(
                f"the codex CLI failed with exit code {completed.returncode}: "
                f"{reason[:500]}", "prompt",
            )
        try:
            output = output_path.read_text(encoding="utf-8")
            return _first_json_object(output)
        except (OSError, json.JSONDecodeError, AttributeError) as exc:
            raise AgentError(
                f"the codex CLI did not return usable JSON: {exc}", "prompt"
            ) from exc


def _first_json_object(text: str) -> dict:
    """Decode one object, tolerating a second object concatenated after it."""
    value, _ = json.JSONDecoder().raw_decode(text.lstrip())
    if not isinstance(value, dict):
        raise json.JSONDecodeError("top-level value is not an object", text, 0)
    return value
