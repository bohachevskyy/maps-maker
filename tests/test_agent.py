"""Agent tests. No network, no API key, no tokens spent."""

import json

import pytest

from mapsvc import agent, harvest as H
from mapsvc.manifest import Manifest

GOOD = {
    "region": "europe", "level": "admin_0",
    "variable": {"source": "natural_earth", "id": "GDP_MD"},
    "normalize": "POP_EST",
    "classify": {"method": "quantile", "k": 5},
    "ramp": "YlGnBu", "projection": "auto", "missing": "hatch",
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
    monkeypatch.setattr(agent, "_region_vocabulary", lambda: "CONTINENT values: Europe.")

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
    from mapsvc import colors, registry
    props = agent.schema()["properties"]["manifest"]["properties"]
    assert props["variable"]["properties"]["id"]["enum"] == sorted(registry.VARIABLES)
    assert props["ramp"]["enum"] == sorted(colors.RAMPS)
    assert props["classify"]["properties"]["method"]["enum"] == list(registry.METHODS)
    assert props["projection"]["enum"] == list(registry.PROJECTIONS)
    assert props["missing"]["enum"] == list(registry.MISSING_MODES)


def test_k_is_an_enum_because_strict_mode_ignores_numeric_ranges():
    from mapsvc import registry
    k = agent.schema()["properties"]["manifest"]["properties"]["classify"]["properties"]["k"]
    assert k["enum"] == list(range(registry.K_MIN, registry.K_MAX + 1))
    assert "minimum" not in k


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
    assert manifest.normalize == "POP_EST"
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


def test_the_model_is_configurable(monkeypatch, stub):
    client = stub(reply())
    monkeypatch.setenv("OPENAI_MODEL", "gpt-5.6-luna")
    agent.describe("anything")
    assert client.responses.calls[0]["model"] == "gpt-5.6-luna"


def test_the_default_model_is_terra(monkeypatch):
    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    assert agent.model_name() == "gpt-5.6-terra"


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
    """Structured Outputs cannot express the nominal/sequential rule, so the
    validator's rejection is handed back to the model."""
    broken = {**GOOD, "normalize": None,
              "variable": {"source": "natural_earth", "id": "SUBREGION"},
              "ramp": "YlGnBu"}
    fixed = {**broken, "ramp": "Set2"}
    client = stub(reply(broken), reply(fixed))

    manifest, raw, _ = agent.describe("show me subregions of Europe")
    assert manifest.ramp == "Set2"
    assert len(client.responses.calls) == 2

    followup = client.responses.calls[1]["input"][-1]["content"]
    assert "rejected" in followup and "ramp" in followup


def test_repeated_failure_gives_up_and_names_the_field(stub):
    broken = {**GOOD, "normalize": None,
              "variable": {"source": "natural_earth", "id": "SUBREGION"},
              "ramp": "YlGnBu"}
    client = stub(reply(broken), reply(broken))
    with pytest.raises(agent.AgentError) as excinfo:
        agent.describe("subregions")
    assert excinfo.value.field == "ramp"
    assert len(client.responses.calls) == 2


def test_the_agent_output_is_never_trusted_without_validation(stub):
    """A manifest the schema permits but the validator rejects must not pass."""
    stub(reply({**GOOD, "level": "admin_0", "region": ""}),
         reply({**GOOD, "region": ""}))
    with pytest.raises(agent.AgentError) as excinfo:
        agent.describe("a map of nowhere")
    assert excinfo.value.field == "region"


# --- configuration --------------------------------------------------------

def test_a_missing_api_key_is_a_deployment_problem_not_a_bad_request(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(agent.AgentUnavailable) as excinfo:
        agent.describe("anything")
    assert "OPENAI_API_KEY" in str(excinfo.value)
    assert "/map" in str(excinfo.value)


def test_an_empty_prompt_is_rejected_before_any_api_call(monkeypatch):
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
