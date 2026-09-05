import copy
import json

import pytest
from fastapi.testclient import TestClient

from mapsvc import harvest as H
from mapsvc.api import app
from tests.test_determinism import RAW, _features


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("MAPSVC_CACHE", str(tmp_path))
    monkeypatch.setattr(H, "load_source", lambda level="admin_0": {"features": _features()})
    return TestClient(app)


def test_post_map_returns_svg(client):
    response = client.post("/map", json=copy.deepcopy(RAW))
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/svg+xml"
    assert response.text.startswith('<?xml version="1.0" encoding="UTF-8"?>')
    assert "<svg" in response.text


def test_an_inverted_bbox_is_422_naming_the_bbox(client):
    """The failure that replaced the ramp rule: a window the wrong way round
    would render a convincing map of the wrong place."""
    response = client.post("/map", json={**copy.deepcopy(RAW),
                                         "bbox": [40.0, 35.0, -5.0, 60.0]})
    assert response.status_code == 422
    assert response.json()["field"] == "bbox"


def test_validation_errors_name_their_field(client):
    cases = [
        ({"bbox": [40.0, 35.0, -5.0, 60.0]}, "bbox"),
        ({"level": "admin_2"}, "level"),      # natural_earth cannot serve it
        ({"level": "admin_9"}, "level"),
        ({"variable": {"source": "natural_earth", "id": "GDP_PPP"}}, "variable.id"),
        ({"basemap": {"source": "gadm"}}, "basemap.source"),
    ]
    for override, field in cases:
        response = client.post("/map", json={**copy.deepcopy(RAW), **override})
        assert response.status_code == 422, override
        assert response.json()["field"] == field


def test_a_window_with_nothing_in_it_is_422(client):
    response = client.post("/map", json={**copy.deepcopy(RAW),
                                         "bbox": [100.0, 60.0, 110.0, 70.0]})
    assert response.status_code == 422
    assert response.json()["field"] == "bbox"


def test_malformed_json_is_422_not_a_crash(client):
    response = client.post("/map", content=b"{not json",
                           headers={"content-type": "application/json"})
    assert response.status_code == 422
    assert set(response.json()) == {"error", "field"}


def test_error_body_is_only_error_and_field(client):
    response = client.post("/map", json={**copy.deepcopy(RAW), "level": "admin_1"})
    assert set(response.json()) == {"error", "field"}


def test_second_identical_request_is_byte_identical(client):
    first = client.post("/map", json=copy.deepcopy(RAW))
    second = client.post("/map", json=copy.deepcopy(RAW))
    assert first.content == second.content


def test_there_is_no_get_endpoint(client):
    assert client.get("/map").status_code == 405
    assert client.get("/").status_code == 404
    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").status_code == 404


# --- POST /describe -------------------------------------------------------

@pytest.fixture
def described(monkeypatch, tmp_path):
    """A client whose agent is stubbed; no API key, no network."""
    from mapsvc import agent
    monkeypatch.setenv("MAPSVC_CACHE", str(tmp_path))
    monkeypatch.setattr(H, "load_source", lambda level="admin_0": {"features": _features()})

    def install(fn):
        monkeypatch.setattr("mapsvc.api.describe", fn)
        return TestClient(app)
    install.agent = agent
    return install


def test_describe_returns_manifest_svg_and_reasoning(described):
    from mapsvc.manifest import validate
    raw = copy.deepcopy(RAW)
    client = described(lambda p: (validate(raw), raw, "GDP by country"))

    response = client.post("/describe", json={"prompt": "GDP per capita in Europe"})
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"manifest", "svg", "reasoning"}
    assert body["manifest"] == raw
    assert body["svg"].startswith('<?xml version="1.0" encoding="UTF-8"?>')
    assert "<svg" in body["svg"]
    assert body["reasoning"] == "GDP by country"


def test_the_returned_manifest_round_trips_through_map(described):
    """The caller must be able to edit what the agent produced and re-post it."""
    from mapsvc.manifest import validate
    raw = copy.deepcopy(RAW)
    client = described(lambda p: (validate(raw), raw, ""))

    described_svg = client.post("/describe", json={"prompt": "anything"}).json()["svg"]
    direct = client.post("/map", json=raw)
    assert direct.status_code == 200
    assert direct.text == described_svg


def test_describe_refusal_is_422_naming_the_prompt(described):
    from mapsvc.agent import AgentError

    def refuse(prompt):
        raise AgentError("nothing in the registry measures rainfall", "prompt")
    client = described(refuse)

    response = client.post("/describe", json={"prompt": "rainfall in Europe"})
    assert response.status_code == 422
    assert response.json() == {"error": "nothing in the registry measures rainfall",
                               "field": "prompt"}


def test_describe_without_an_api_key_is_503_not_422(described):
    """Not the caller's fault, so not a 4xx."""
    from mapsvc.agent import AgentUnavailable

    def unconfigured(prompt):
        raise AgentUnavailable("OPENAI_API_KEY is not set")
    client = described(unconfigured)

    response = client.post("/describe", json={"prompt": "GDP in Europe"})
    assert response.status_code == 503
    assert "OPENAI_API_KEY" in response.json()["error"]


def test_describe_rejects_malformed_bodies(described):
    client = described(lambda p: None)
    assert client.post("/describe", content=b"{oops",
                       headers={"content-type": "application/json"}).status_code == 422
    assert client.post("/describe", json=["a", "b"]).status_code == 422


def test_describe_has_no_get(described):
    client = described(lambda p: None)
    assert client.get("/describe").status_code == 405


# --- POST /export ---------------------------------------------------------

@pytest.fixture
def exporter(monkeypatch, tmp_path):
    monkeypatch.setenv("MAPSVC_CACHE", str(tmp_path / "cache"))
    monkeypatch.setenv("MAPSVC_OUTPUT", str(tmp_path / "maps"))
    monkeypatch.setattr(H, "load_source", lambda level="admin_0": {"features": _features()})
    return TestClient(app), tmp_path / "maps"


def test_export_accepts_a_bare_manifest_and_writes_a_file(exporter):
    import pathlib
    client, outdir = exporter
    response = client.post("/export", json=copy.deepcopy(RAW))
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"path", "manifest", "bytes", "overwrote", "reasoning"}

    path = pathlib.Path(body["path"])
    assert path.is_absolute()
    assert path.parent == outdir
    assert path.exists()
    assert path.read_text().startswith('<?xml version="1.0" encoding="UTF-8"?>')
    assert len(path.read_bytes()) == body["bytes"]
    assert body["reasoning"] is None, "no agent involved, so nothing to explain"
    assert body["overwrote"] is False


def test_export_accepts_a_wrapped_manifest(exporter):
    client, _ = exporter
    response = client.post("/export", json={"manifest": copy.deepcopy(RAW)})
    assert response.status_code == 200
    assert response.json()["manifest"] == RAW


def test_export_accepts_a_prompt_and_reports_the_reasoning(exporter):
    import pathlib
    client, outdir = exporter
    raw = copy.deepcopy(RAW)
    client.app  # keep ref
    from mapsvc.manifest import validate as _validate
    import mapsvc.api as api_module
    original = api_module.describe
    api_module.describe = lambda p: (_validate(raw), raw, "inferred per capita")
    try:
        response = client.post("/export", json={"prompt": "GDP per capita in Europe"})
    finally:
        api_module.describe = original

    assert response.status_code == 200
    body = response.json()
    assert body["reasoning"] == "inferred per capita"
    assert pathlib.Path(body["path"]).exists()


def test_export_is_idempotent(exporter):
    client, outdir = exporter
    first = client.post("/export", json=copy.deepcopy(RAW)).json()
    second = client.post("/export", json=copy.deepcopy(RAW)).json()
    assert first["path"] == second["path"]
    assert second["overwrote"] is True
    assert len(list(outdir.iterdir())) == 1


def test_export_file_matches_what_map_returns(exporter):
    import pathlib
    client, _ = exporter
    exported = client.post("/export", json=copy.deepcopy(RAW)).json()
    rendered = client.post("/map", json=copy.deepcopy(RAW))
    assert pathlib.Path(exported["path"]).read_text() == rendered.text


def test_export_validation_errors_match_map(exporter):
    client, outdir = exporter
    response = client.post("/export", json={**copy.deepcopy(RAW), "level": "admin_2"})
    assert response.status_code == 422
    assert response.json()["field"] == "level"
    assert not outdir.exists() or not list(outdir.iterdir()), "no file on failure"


def test_export_rejects_malformed_bodies(exporter):
    client, _ = exporter
    assert client.post("/export", content=b"{oops",
                       headers={"content-type": "application/json"}).status_code == 422
    assert client.post("/export", json=[1, 2]).status_code == 422


def test_export_has_no_get(exporter):
    client, _ = exporter
    assert client.get("/export").status_code == 405
