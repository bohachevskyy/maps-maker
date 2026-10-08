"""Agent tests. No network, no API key, no tokens spent."""

import json
import pathlib
import subprocess

import pytest

from mapsvc import agent, harvest as H
from mapsvc.manifest import Manifest

GOOD = {
    "bbox": [-5.0, 35.0, 40.0, 60.0], "level": "admin_0",
    "variable": {"source": "natural_earth", "id": "GDP_MD"},
    }


class FakeResponses:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        reply = self.replies.pop(0)
        return type("R", (), {"output_text": json.dumps(reply)})()


class FakeClient:
    def __init__(self, replies):
        self.responses = FakeResponses(replies)


@pytest.fixture
def stub(monkeypatch, tmp_path):
    """Replace the OpenAI client with canned replies."""
    monkeypatch.setenv("MAPSVC_CACHE", str(tmp_path))
    monkeypatch.setenv("MAPSVC_LLM", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    def install(*replies):
        client = FakeClient(replies)
        monkeypatch.setattr(agent, "_client", lambda: client)
        return client
    return install


def reply(manifest=None, mappable=True, refusal=None, reasoning="because"):
    return {"mappable": mappable, "refusal": refusal, "reasoning": reasoning,
            "manifest": manifest or GOOD}


# --- schema ---------------------------------------------------------------

def test_schema_enums_come_from_the_registry():
    from mapsvc import statistics
    props = agent.schema()["properties"]["manifest"]["properties"]
    assert props["variable"]["properties"]["source"]["enum"] == list(statistics.sources())
    # variable.id is deliberately NOT an enum: a searched id cannot be listed
    # ahead of time, and an enum would forbid the answers search exists to find.
    assert "enum" not in props["variable"]["properties"]["id"]
    every = set(statistics.fixed_variables())
    assert set(statistics.variables_for("admin_0", "natural_earth")) < every
    # owid is searchable, so it contributes no fixed entries -- its ids come
    # from a live search instead.
    assert statistics.variables_for("admin_0", "owid") == {}






def test_the_schema_has_no_render_time_fields():
    """Colour, classification, projection and missing handling are derived, so
    there is nothing for the model to get wrong about them."""
    props = agent.schema()["properties"]["manifest"]["properties"]
    for gone in ("ramp", "classify", "projection", "missing", "normalize", "region"):
        assert gone not in props


def test_bbox_is_the_window():
    props = agent.schema()["properties"]["manifest"]["properties"]
    assert props["bbox"]["type"] == "array"
    assert "min_lon" in props["bbox"]["description"]


def test_schema_obeys_strict_mode_structural_rules():
    """Every object must list all properties as required and forbid extras.

    Checked here because a violation only surfaces as an API error at runtime,
    and there is no key in CI to discover it with.
    """
    def walk(node, path="root"):
        if not isinstance(node, dict):
            return
        if node.get("type") == "object":
            assert node.get("additionalProperties") is False, f"{path}: extras allowed"
            assert set(node.get("required", [])) == set(node.get("properties", {})), \
                f"{path}: required must list every property"
        for key, child in node.get("properties", {}).items():
            walk(child, f"{path}.{key}")
    walk(agent.schema())


def test_schema_avoids_anyof_which_strict_mode_handles_poorly():
    assert "anyOf" not in json.dumps(agent.schema())


# --- happy path -----------------------------------------------------------

def test_a_plain_request_produces_a_validated_manifest(stub):
    stub(reply())
    manifest, raw, reasoning = agent.describe("GDP per capita in Europe")
    assert isinstance(manifest, Manifest)
    assert manifest.variable_id == "GDP_MD"
    assert manifest.bbox == (-5.0, 35.0, 40.0, 60.0)
    assert raw == GOOD
    assert reasoning == "because"


def test_the_prompt_reaches_the_model_with_the_schema_attached(stub):
    client = stub(reply())
    agent.describe("  GDP per capita in Europe  ")
    call = client.responses.calls[0]
    assert call["input"] == [{"role": "user", "content": "GDP per capita in Europe"}]
    assert call["text"]["format"]["strict"] is True
    assert call["text"]["format"]["schema"] == agent.schema()
    assert "nominal" in call["instructions"]
    assert "Germany (within [\"DEU\"])" in call["instructions"]
    assert "prefer BLS LAUS" in call["instructions"]
    assert "nuts_1, nuts_2, nuts_3" in call["instructions"]


def test_the_model_is_configurable(monkeypatch, stub):
    client = stub(reply())
    monkeypatch.setenv("OPENAI_MODEL", "gpt-5.6-luna")
    agent.describe("anything")
    assert client.responses.calls[0]["model"] == "gpt-5.6-luna"


def test_the_default_model_is_terra(monkeypatch):
    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    assert agent.model_name() == "gpt-5.6-terra"


def test_backend_defaults_to_codex_only_without_a_key_when_it_is_on_path(monkeypatch):
    monkeypatch.delenv("MAPSVC_LLM", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(agent.shutil, "which", lambda name: "/bin/codex")
    assert agent.backend_name() == "codex"

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    assert agent.backend_name() == "openai"


def test_codex_backend_uses_schema_transcript_and_first_json_object(monkeypatch):
    monkeypatch.setenv("MAPSVC_LLM", "codex")
    monkeypatch.setenv("MAPSVC_CODEX_MODEL", "test-model")
    monkeypatch.setattr(agent.shutil, "which", lambda name: "/bin/codex")
    seen = {}

    def run(command, **kwargs):
        seen["command"] = command
        seen["kwargs"] = kwargs
        schema_path = command[command.index("--output-schema") + 1]
        output_path = command[command.index("-o") + 1]
        assert json.loads(pathlib.Path(schema_path).read_text()) == agent.schema()
        payload = json.dumps(reply())
        pathlib.Path(output_path).write_text(payload + payload)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(agent.subprocess, "run", run)
    manifest, _, _ = agent.describe("GDP per capita in Europe")

    assert manifest.variable_id == "GDP_MD"
    assert seen["command"][:3] == ["/bin/codex", "exec", "--ephemeral"]
    assert seen["command"][-1] == "-"
    assert seen["command"][seen["command"].index("-m") + 1] == "test-model"
    assert seen["kwargs"]["timeout"] == 180
    assert "USER: GDP per capita in Europe" in seen["kwargs"]["input"]
    assert "You assemble manifests" in seen["kwargs"]["input"]


def test_codex_backend_defaults_to_terra(monkeypatch):
    monkeypatch.setenv("MAPSVC_LLM", "codex")
    monkeypatch.delenv("MAPSVC_CODEX_MODEL", raising=False)
    monkeypatch.setattr(agent.shutil, "which", lambda name: "/bin/codex")
    seen = {}

    def run(command, **kwargs):
        seen["command"] = command
        pathlib.Path(command[command.index("-o") + 1]).write_text(json.dumps(reply()))
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(agent.subprocess, "run", run)
    agent.describe("anything")
    assert seen["command"][seen["command"].index("-m") + 1] == "gpt-5.6-terra"


def test_codex_timeout_is_an_agent_error(monkeypatch):
    monkeypatch.setenv("MAPSVC_LLM", "codex")
    monkeypatch.setattr(agent.shutil, "which", lambda name: "/bin/codex")

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])

    monkeypatch.setattr(agent.subprocess, "run", timeout)
    with pytest.raises(agent.AgentError, match="timed out") as excinfo:
        agent.describe("anything")
    assert excinfo.value.field == "prompt"


# --- refusal --------------------------------------------------------------

def test_an_unmappable_request_is_refused_not_substituted(stub):
    stub(reply(mappable=False,
               refusal="nothing here measures rainfall; available: GDP_MD, POP_EST"))
    with pytest.raises(agent.AgentError) as excinfo:
        agent.describe("rainfall in Europe")
    assert excinfo.value.field == "prompt"
    assert "rainfall" in str(excinfo.value)


def test_a_refusal_without_a_reason_still_fails_cleanly(stub):
    stub(reply(mappable=False, refusal=None))
    with pytest.raises(agent.AgentError):
        agent.describe("the mood of each country")


# --- repair loop ----------------------------------------------------------

def test_a_rejected_manifest_is_repaired_on_a_second_attempt(stub):
    """The nominal/sequential rule is unrepresentable now, but wrong-level use
    still is: `type` is an admin_1 column and cannot be mapped at admin_0."""
    broken = {**GOOD, "variable": {"source": "natural_earth", "id": "type",
                                   "search_query": ""}}
    fixed = {**GOOD, "variable": {"source": "natural_earth", "id": "SUBREGION",
                                  "search_query": ""}}
    client = stub(reply(broken), reply(fixed))

    manifest, _, _ = agent.describe("show me subregions of Europe")
    assert manifest.variable_id == "SUBREGION"
    assert len(client.responses.calls) == 2

    followup = client.responses.calls[1]["input"][-1]["content"]
    assert "rejected" in followup and "variable.id" in followup


def test_an_overpass_failure_is_fed_to_the_repair_turn(stub, monkeypatch):
    broken = {**GOOD, "overlays": [
        {"source": "overpass", "id": "service=highspeed", "search_query": ""}
    ]}
    fixed = dict(GOOD)
    client = stub(reply(broken), reply(fixed))

    def reject(*args, **kwargs):
        raise agent.overlays.OverlayError(
            "OSM tag service=highspeed has only 19 uses worldwide; check the tag",
            "overlays.id")

    from mapsvc.overlays import overpass
    monkeypatch.setattr(overpass, "load", reject)
    agent.describe("French high-speed rail")
    followup = client.responses.calls[1]["input"][-1]["content"]
    assert "service=highspeed" in followup and "overlays[0].id" in followup


def test_a_clipped_within_bbox_is_fed_to_the_repair_turn(stub, monkeypatch):
    broken = {**GOOD, "within": ["USA"]}
    fixed = {key: value for key, value in broken.items() if key != "bbox"}
    client = stub(reply(broken), reply(fixed))
    calls = 0

    def validate_window(manifest):
        nonlocal calls
        calls += 1
        if manifest.bbox is not None:
            raise H.HarvestError("bbox cuts off Alaska, Hawaii", "bbox")

    monkeypatch.setattr(H, "validate_window", validate_window)
    agent.describe("diesel use by US state")
    followup = client.responses.calls[1]["input"][-1]["content"]
    assert "Alaska, Hawaii" in followup and "field: bbox" in followup


def test_repeated_failure_gives_up_and_names_the_field(stub):
    broken = {**GOOD, "variable": {"source": "natural_earth", "id": "type",
                                   "search_query": ""}}
    client = stub(reply(broken), reply(broken), reply(broken))
    with pytest.raises(agent.AgentError) as excinfo:
        agent.describe("subregions")
    assert excinfo.value.field == "variable.id"


def test_the_agent_output_is_never_trusted_without_validation(stub):
    """A manifest the schema permits but the validator rejects must not pass.
    Structured Outputs cannot express "min_lon must be west of max_lon"."""
    inverted = {**GOOD, "bbox": [40.0, 35.0, -5.0, 60.0]}
    stub(reply(inverted), reply(inverted), reply(inverted))
    with pytest.raises(agent.AgentError) as excinfo:
        agent.describe("a map of nowhere")
    assert excinfo.value.field == "bbox"


# --- configuration --------------------------------------------------------

def test_a_missing_api_key_is_a_deployment_problem_not_a_bad_request(monkeypatch):
    monkeypatch.setenv("MAPSVC_LLM", "openai")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(agent.AgentUnavailable) as excinfo:
        agent.describe("anything")
    assert "OPENAI_API_KEY" in str(excinfo.value)
    assert "/map" in str(excinfo.value)


def test_no_available_default_backend_is_unavailable(monkeypatch):
    monkeypatch.delenv("MAPSVC_LLM", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(agent.shutil, "which", lambda name: None)
    with pytest.raises(agent.AgentUnavailable):
        agent.describe("anything")


def test_an_empty_prompt_is_rejected_before_any_api_call(monkeypatch):
    monkeypatch.setenv("MAPSVC_LLM", "openai")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    for bad in ("", "   ", None, 42):
        with pytest.raises(agent.AgentError) as excinfo:
            agent.describe(bad)
        assert excinfo.value.field == "prompt"


def test_unparseable_model_output_is_an_error_not_a_crash(stub, monkeypatch):
    class Broken:
        responses = type("R", (), {"create": lambda self, **kw: type(
            "X", (), {"output_text": "not json"})()})()
    monkeypatch.setattr(agent, "_client", lambda: Broken())
    with pytest.raises(agent.AgentError):
        agent.describe("anything")


def test_map_endpoint_does_not_need_the_openai_sdk():
    """/map must keep working with no key and no SDK, so the import is lazy."""
    import ast
    import pathlib
    tree = ast.parse((pathlib.Path(agent.__file__)).read_text())
    toplevel = {a.name.split(".")[0] for n in tree.body if isinstance(n, ast.Import)
                for a in n.names}
    toplevel |= {n.module.split(".")[0] for n in tree.body
                 if isinstance(n, ast.ImportFrom) and n.module}
    assert "openai" not in toplevel


# --- per and change -------------------------------------------------------

NONE_PER = {"source": "", "id": ""}
NONE_CHANGE = {"from": 0, "to": 0}


def test_empty_per_and_change_mean_absent(stub):
    raw = {**GOOD, "variable": {**GOOD["variable"], "search_query": "",
                                "per": NONE_PER, "change": NONE_CHANGE}}
    stub(reply(raw))
    manifest, cleaned, _ = agent.describe("GDP in Europe")
    assert manifest.per is None and manifest.change is None
    assert set(cleaned["variable"]) == {"source", "id"}


def test_a_searchable_divisor_must_be_a_declared_denominator(stub, monkeypatch):
    """The agent never saw a search for the divisor, so only the source's
    declared denominators are fair game -- anything else is sent back."""
    from mapsvc.statistics import eia
    monkeypatch.setattr(eia, "search", lambda query, limit=8: [
        agent.statistics.Candidate(id="DFTCP", title="Distillate fuel oil total consumption")])
    first = {"within": ["USA"], "level": "admin_1",
             "basemap": {"source": "natural_earth", "detail": "simplified"},
             "variable": {"source": "eia", "id": "", "search_query": "diesel",
                          "per": NONE_PER, "change": NONE_CHANGE}}
    invented = {**first, "variable": {"source": "eia", "id": "DFTCP", "search_query": "",
                                      "per": {"source": "eia", "id": "POPULATION"},
                                      "change": NONE_CHANGE}}
    declared = {**first, "variable": {"source": "eia", "id": "DFTCP", "search_query": "",
                                      "per": {"source": "eia", "id": "TPOPP"},
                                      "change": NONE_CHANGE}}
    client = stub(reply(first), reply(invented), reply(declared))
    manifest, _, _ = agent.describe("diesel per person by US state")
    assert manifest.per == ("eia", "TPOPP")
    assert "TPOPP" in client.responses.calls[2]["input"][-1]["content"]


def test_change_reaches_the_manifest(stub, monkeypatch):
    from mapsvc.statistics import owid
    monkeypatch.setattr(owid, "search", lambda query, limit=8: [
        agent.statistics.Candidate(id="population", title="Population")])
    first = {"bbox": [-180, -90, 180, 90], "level": "admin_0",
             "basemap": {"source": "natural_earth", "detail": "simplified"},
             "variable": {"source": "owid", "id": "", "search_query": "population",
                          "per": NONE_PER, "change": NONE_CHANGE}}
    chosen = {**first, "variable": {"source": "owid", "id": "population", "search_query": "",
                                    "per": NONE_PER, "change": {"from": 2000, "to": 2023}}}
    stub(reply(first), reply(chosen))
    manifest, _, _ = agent.describe("population change 2000-2023")
    assert manifest.change == (2000, 2023)


def test_the_instructions_explain_keys_and_derived_variables():
    text = agent.instructions()
    for needle in ("JOIN KEYS", "census", "gisco", "variable.per", "variable.change",
                   "TPOPP", "median household income", "FIPS", "NUTS"):
        assert needle in text, needle
