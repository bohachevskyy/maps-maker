"""Turning a customer's sentence into a manifest.

The agent's only job is assembly. It does not render, and it is not trusted:
whatever it produces goes through `manifest.validate` exactly like a
hand-written manifest, and a rejection is fed back for one repair attempt.

The JSON schema is generated from `registry.py`, so Structured Outputs makes it
structurally impossible to emit an unknown variable, ramp, method, projection or
k. Adding a variable to the registry extends what the agent can say with no
prompt edits. What the schema cannot express is the cross-field rule -- that a
nominal variable rejects a sequential ramp -- which is why the validator stays
in the loop.
"""

import functools
import json
import os

from mapsvc import colors, harvest, registry, statistics
from mapsvc.manifest import Manifest, ManifestError, validate
from mapsvc.statistics import StatisticsError

DEFAULT_MODEL = "gpt-5.6-terra"
MAX_OUTPUT_TOKENS = 2000
REPAIR_ATTEMPTS = 1
SEARCH_RESULTS = 8


class AgentError(ValueError):
    """The prompt could not be turned into a valid manifest."""

    def __init__(self, message: str, field: str):
        super().__init__(message)
        self.field = field


class AgentUnavailable(RuntimeError):
    """The agent is not configured -- a deployment problem, not a bad request."""


def model_name() -> str:
    return os.environ.get("OPENAI_MODEL", DEFAULT_MODEL)


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
    ramps = sorted(colors.RAMPS)
    manifest_properties = {
        "region": {"type": "string",
                   "description": "'world', a CONTINENT name, or an ADM0_A3 code"},
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
            },
            "required": ["source", "id", "search_query"],
            "additionalProperties": False,
        },
        # null means a base map: boundaries with nothing painted on them.
        "variable_is_null": {
            "type": "boolean",
            "description": "true for a base map -- draw the boundaries, shade nothing",
        },
        "normalize": {"type": ["string", "null"],
                      "enum": sorted(statistics.fixed_variables()) + [None]},
        "classify": {
            "type": "object",
            "properties": {
                "method": {"type": "string", "enum": list(registry.METHODS)},
                # An enum rather than minimum/maximum: strict mode honours
                # enums, and ignores numeric range keywords.
                "k": {"type": "integer",
                      "enum": list(range(registry.K_MIN, registry.K_MAX + 1))},
            },
            "required": ["method", "k"],
            "additionalProperties": False,
        },
        "ramp": {"type": "string", "enum": ramps},
        "projection": {"type": "string", "enum": list(registry.PROJECTIONS)},
        "missing": {"type": "string", "enum": list(registry.MISSING_MODES)},
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


@functools.lru_cache(maxsize=1)
def _region_vocabulary() -> str:
    """Continents and country codes actually present in the data.

    ADM0_A3 is mostly ISO3 but carries custom codes for disputed and
    non-sovereign entities, so the real list beats the model's recollection.
    """
    features = harvest.load_source("admin_0")["features"]
    continents = sorted({f["properties"][registry.continent_property("admin_0")]
                         for f in features})
    codes = sorted({(f["properties"][registry.id_property("admin_0")],
                     f["properties"].get("NAME", "")) for f in features})
    listed = ", ".join(f"{code} ({name})" for code, name in codes)
    return (
        f"CONTINENT values: {', '.join(continents)}.\n"
        f"ADM0_A3 codes: {listed}."
    )


def _describe(variables: dict) -> str:
    return "\n".join(
        f"  {name}: {meta.level}"
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
        block = (f"  source {c.source!r} -- {kind}, levels "
                 f"{', '.join(c.levels)}.\n    {c.description}")
        if not c.searchable:
            # List a fixed source's whole catalogue, whichever source it is --
            # naming natural_earth here would leave a newly registered fixed
            # source invisible to the agent.
            for level in c.levels:
                listed = _describe(statistics.variables_for(level, c.source))
                if listed:
                    block += f"\n    at {level}:\n{listed}"
        blocks.append(block)
    sources = "\n\n".join(blocks)
    by_kind: dict[str, list[str]] = {}
    for name, kind in sorted(colors.RAMPS.items()):
        by_kind.setdefault(kind, []).append(name)
    ramps = "\n".join(f"  {kind}: {', '.join(names)}" for kind, names in sorted(by_kind.items()))

    return f"""You assemble manifests for a choropleth map service. You choose what to map; \
you never draw anything.

Boundaries and statistics come from separate places. `level` picks how fine the \
units are, `basemap` picks who supplies their outlines, and `variable` picks \
what -- if anything -- is painted on them.

level "admin_0" -- countries. Six mappable variables:

{variables}

level "admin_1" -- sub-national units: oblasts, states, provinces, regions, \
departments, prefectures. Choose this whenever the request is about units \
*inside* a country. Four mappable variables:

{admin1}

levels "admin_2" and "admin_3" -- finer still: raions, counties, districts, \
localities. These exist only on the overture basemap and have no variables at \
all, so they are always base maps.

The basemap decides where the polygons come from, and it is separate from where \
the numbers come from:

  overture      -- the default. admin_0 through admin_3, far the most detail. \
Queried live from S3, so the first request for a given map takes about ten \
seconds; afterwards it is cached.
  natural_earth -- admin_0 and admin_1 only, much coarser, but instant and \
already on disk. Choose it when the user asks for something fast, rough or \
low-resolution, or for a whole continent or the world at once, where Overture \
is slow enough to be a problem.

Set variable_is_null to true for a base map: boundaries drawn with no shading. \
That is the right answer whenever someone asks to *see* or *draw* units rather \
than to compare a quantity across them, and it is the only possible answer at \
admin_2 and admin_3.

Admin-1 carries no statistics at all: no population, no GDP, no income. Its 121 \
properties are classification and cartographic metadata, and area_sqkm is zero \
for every unit on earth. An admin-1 map can therefore show *where* the units are \
and *what kind* they are, and nothing else. If someone asks for population or \
economics by oblast or by state, do not refuse as though the places did not \
exist -- the boundaries are there; it is the statistic that is missing. Say \
exactly that.

A variable belongs to exactly one level: GDP_MD at admin_1, or type at admin_0, \
is rejected.

Statistics come from a source, named in variable.source. Sources are of two \
kinds and you use them differently:

{sources}

FIXED sources are small and fully listed above. Put the id straight into \
variable.id and leave search_query empty.

SEARCHABLE sources are far too large to list. Do NOT guess an id for one. \
Leave variable.id empty and put a plain-English phrase naming the measure into \
variable.search_query -- "unemployment rate", "deaths from air pollution", \
"share of land that is forest". The service will run the search and come back \
with real candidates; you then pick one of those ids exactly. Choosing an id \
that was not offered will fail.

Write the query as the measure itself, not as the user's sentence. "How many \
people are out of work in Europe?" should search for "unemployment rate", not \
for the whole question.

Prefer the source that actually publishes what was asked. Population, GDP, \
income group, economy and subregion are natural_earth. Almost anything else \
about people, health, environment, energy or politics is worth searching for.

Colour ramps:

{ramps}

Rules you must follow:

1. A nominal variable (SUBREGION, type, type_en, region) must use a qualitative \
ramp. Shading unordered categories light-to-dark claims one is "more" than \
another. Ordinal and count variables should normally use a sequential ramp.
   If the user asks for a ramp that breaks this rule, do NOT refuse. Pick a \
valid ramp, produce the map, and say in reasoning why you overrode them. A bad \
ramp choice is correctable; refusing a request you can actually satisfy is not.
2. Choose a qualitative ramp with at least as many colours as the variable has \
categories in the requested region. SUBREGION has 22 categories worldwide but \
only 4 in Europe; no qualitative ramp holds more than 12. Refuse only when no \
available ramp is large enough -- that is a request you cannot satisfy, unlike \
rule 1 where a valid alternative exists.
3. Set normalize when the request implies a rate rather than a total: \
"per capita", "per person", "how rich", "density". GDP_MD normalised by \
POP_EST is GDP per capita. Leave it null for totals like "total population".
4. quantile suits skewed data and is a safe default. equal_interval suits evenly \
spread data and is a poor choice for GDP or population, which are heavily \
right-skewed. jenks finds natural groupings.
5. k must be 3-9; 5 is a reasonable default.
6. Prefer projection "auto" unless the user names one.
7. missing "hatch" is the default; use "exclude" only if asked to omit \
countries without data.

Region is a filter, not geography. At admin_0 it is "world", one CONTINENT \
value, or one ADM0_A3 code; at admin_1 it is "world" or one ADM0_A3 code, \
because admin-1 features carry no continent. "world" at admin_1 means 4,596 \
units and a very large file, so prefer a single country unless the whole planet \
is genuinely wanted. There is no sub-continental grouping: "Scandinavia", "the Balkans" \
and "the EU" are not regions. If asked for one, either refuse or pick a single \
country code, and say which you did in reasoning.

{_region_vocabulary()}

Refusal is about *what is being mapped*, never about how it is drawn. Set \
mappable to false only when no variable answers the request -- rainfall, \
unemployment, life expectancy, elections -- or when no available ramp is large \
enough for the categories. Say so plainly and name the variables that do exist. \
Do not substitute a loosely related variable and hope the caller notices; \
returning no map is better than returning a map of the wrong thing.

Never refuse over a field you are free to set yourself. The ramp, the \
projection, the classification method, k and the missing mode are all yours to \
choose. If the user asks for one of them that you cannot honour, set a correct \
value, return the map, and explain the substitution in reasoning. "You asked for \
a sequential ramp on a nominal variable" is a sentence in reasoning, not a \
refusal."""


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

    client = _client()
    conversation: list[dict] = [{"role": "user", "content": prompt.strip()}]
    last_error: ManifestError | None = None
    offered: set[str] | None = None

    for attempt in range(REPAIR_ATTEMPTS + 2):
        answer = _ask(client, conversation)

        if not answer.get("mappable", False):
            raise AgentError(
                answer.get("refusal") or "this request cannot be mapped from the "
                "available data", "prompt"
            )

        raw = dict(answer["manifest"])
        query = (raw.get("variable") or {}).get("search_query") or ""
        chosen = (raw.get("variable") or {}).get("id") or ""
        source = (raw.get("variable") or {}).get("source")

        if source and not chosen and query and offered is None:
            found = _search(source, query)
            offered = {c.id for c in found}
            conversation.append({"role": "assistant", "content": json.dumps(answer)})
            conversation.append({"role": "user", "content": _candidate_prompt(
                source, query, found)})
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
        if raw.pop("variable_is_null", False):
            raw["variable"] = None
            raw["normalize"] = None
            raw.pop("classify", None)
            raw.pop("ramp", None)
        try:
            return validate(raw), raw, answer.get("reasoning", "")
        except ManifestError as error:
            last_error = error
            if attempt == REPAIR_ATTEMPTS:
                break
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
        "matches the request most directly and has the widest country coverage; "
        "avoid ones that split by sex, age or subcategory unless asked. If none "
        "of them is what was asked for, refuse instead of settling."
    )


def _ask(client, conversation: list[dict]) -> dict:
    response = client.responses.create(
        model=model_name(),
        instructions=instructions(),
        input=conversation,
        max_output_tokens=MAX_OUTPUT_TOKENS,
        text={"format": {"type": "json_schema", "name": "map_request",
                         "strict": True, "schema": schema()}},
    )
    try:
        return json.loads(response.output_text)
    except (json.JSONDecodeError, AttributeError) as exc:
        raise AgentError(f"the model did not return usable JSON: {exc}", "prompt") from exc
