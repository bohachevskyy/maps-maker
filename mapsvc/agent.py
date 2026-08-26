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

from mapsvc import colors, harvest, registry
from mapsvc.manifest import Manifest, ManifestError, validate

DEFAULT_MODEL = "gpt-5.6-terra"
MAX_OUTPUT_TOKENS = 2000
REPAIR_ATTEMPTS = 1


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
        "variable": {
            "type": "object",
            "properties": {
                "source": {"type": "string", "enum": list(registry.SOURCES)},
                "id": {"type": "string", "enum": sorted(registry.VARIABLES)},
            },
            "required": ["source", "id"],
            "additionalProperties": False,
        },
        "normalize": {"type": ["string", "null"],
                      "enum": sorted(registry.VARIABLES) + [None]},
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
    features = harvest.load_source()["features"]
    continents = sorted({f["properties"][registry.CONTINENT_PROPERTY]
                         for f in features})
    codes = sorted({(f["properties"][registry.ID_PROPERTY],
                     f["properties"].get("NAME", "")) for f in features})
    listed = ", ".join(f"{code} ({name})" for code, name in codes)
    return (
        f"CONTINENT values: {', '.join(continents)}.\n"
        f"ADM0_A3 codes: {listed}."
    )


def instructions() -> str:
    variables = "\n".join(
        f"  {name}: {meta['level']}"
        + (f", in {meta['unit']}" if meta["unit"] else "")
        + (f", vintage from {meta['year_col']}" if meta["year_col"] else "")
        for name, meta in registry.VARIABLES.items()
    )
    by_kind: dict[str, list[str]] = {}
    for name, kind in sorted(colors.RAMPS.items()):
        by_kind.setdefault(kind, []).append(name)
    ramps = "\n".join(f"  {kind}: {', '.join(names)}" for kind, names in sorted(by_kind.items()))

    return f"""You assemble manifests for a choropleth map service. You choose what to map; \
you never draw anything.

The only data available is Natural Earth admin-0 country polygons. There are \
exactly six mappable variables:

{variables}

Colour ramps:

{ramps}

Rules you must follow:

1. A nominal variable (SUBREGION) must use a qualitative ramp. Shading unordered \
categories light-to-dark claims one is "more" than another. Ordinal and count \
variables should normally use a sequential ramp.
2. Choose a qualitative ramp with at least as many colours as the variable has \
categories in the requested region. SUBREGION has 22 categories worldwide but \
only 4 in Europe; no qualitative ramp holds more than 12.
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

Region is a filter, not geography. It is "world", one CONTINENT value, or one \
ADM0_A3 code. There is no sub-continental grouping: "Scandinavia", "the Balkans" \
and "the EU" are not regions. If asked for one, either refuse or pick a single \
country code, and say which you did in reasoning.

{_region_vocabulary()}

If nothing in the six variables answers the request -- rainfall, unemployment, \
life expectancy, elections -- set mappable to false and use refusal to say so \
plainly, naming the variables that do exist. Do not substitute a loosely related \
variable and hope the caller notices. Returning no map is better than returning \
a map of the wrong thing."""


def describe(prompt: str) -> tuple[Manifest, dict, str]:
    """Assemble and validate a manifest from a natural-language request.

    Returns the validated manifest, its raw dict form, and the agent's reasoning.
    """
    if not isinstance(prompt, str) or not prompt.strip():
        raise AgentError("prompt must be a non-empty string", "prompt")

    client = _client()
    conversation: list[dict] = [{"role": "user", "content": prompt.strip()}]
    last_error: ManifestError | None = None

    for attempt in range(REPAIR_ATTEMPTS + 1):
        answer = _ask(client, conversation)

        if not answer.get("mappable", False):
            raise AgentError(
                answer.get("refusal") or "this request cannot be mapped from the "
                "available data", "prompt"
            )

        raw = answer["manifest"]
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
