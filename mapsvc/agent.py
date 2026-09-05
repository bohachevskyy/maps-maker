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

from mapsvc import registry, statistics
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
    manifest_properties = {
        "bbox": {
            "type": "array",
            "description": "the map window as [min_lon, min_lat, max_lon, max_lat] "
                           "in degrees. min_lon must be west of max_lon and "
                           "min_lat south of max_lat.",
            "items": {"type": "number"},
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
            },
            "required": ["source", "id", "search_query"],
            "additionalProperties": False,
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
    max_units = f"{registry.MAX_UNITS:,}"
    region_hint = ("Country codes, if you need one for a statistics join: "
                   "they are ISO3.")

    return f"""You assemble manifests for a map service. You choose what to show \
and where; you never choose how it is drawn. Colour, classification, projection \
and missing-value handling are all derived from the data, so there are no fields \
for them and you cannot get them wrong.

A manifest has four things: a window (bbox), a granularity (level), where the \
outlines come from (basemap), and optionally what to shade them by (variable).

THE WINDOW

bbox is [min_lon, min_lat, max_lon, max_lat] in degrees. Work it out from the \
request. You are expected to know roughly where places are:

  Europe          [-25, 34, 45, 72]
  Ukraine         [22, 44, 41, 53]
  the Baltics     [20, 53, 29, 60]
  Benelux         [2.5, 49.4, 7.3, 53.6]
  around Kyiv     [29.2, 49.2, 32.2, 51.6]
  the whole world [-180, -90, 180, 90]

Round to about a tenth of a degree; precision beyond that is false. Pad a little \
so the subject is not flush against the edge. A window that crosses the \
antimeridian is not supported -- for the Pacific, pick one side.

This replaces named regions entirely, which means places with no official code \
now work: "Scandinavia", "the Balkans", "the Horn of Africa", "the area around \
Lviv" are all just windows. There is nothing left to refuse on those grounds.

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
a continent, where Overture is slow enough to be a problem.

A statistics source only publishes at certain levels: everything below admin_1 \
is boundaries only. Set variable_is_null to true for a base map -- boundaries \
with no shading. That is right whenever someone asks to *see* or *draw* units \
rather than compare a quantity, and it is the only possibility at admin_2 and \
admin_3.

REFUSING

Refusal is about what is being measured, never about where or how. Set mappable \
to false only when no source publishes the requested measure -- rainfall, \
election results -- or when a search comes back empty. Say so plainly and name \
what does exist. Never refuse because a place has no official code: every place \
is a window now.

{region_hint}"""


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
