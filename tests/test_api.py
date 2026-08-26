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
    monkeypatch.setattr(H, "load_source", lambda: {"features": _features()})
    return TestClient(app)


def test_post_map_returns_svg(client):
    response = client.post("/map", json=copy.deepcopy(RAW))
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/svg+xml"
    assert response.text.startswith('<?xml version="1.0" encoding="UTF-8"?>')
    assert "<svg" in response.text


def test_nominal_variable_with_a_sequential_ramp_is_422_naming_the_ramp(client):
    body = {**copy.deepcopy(RAW), "normalize": None,
            "variable": {"source": "natural_earth", "id": "SUBREGION"}, "ramp": "YlGnBu"}
    response = client.post("/map", json=body)
    assert response.status_code == 422
    assert response.json()["field"] == "ramp"
    assert "qualitative" in response.json()["error"]


def test_validation_errors_name_their_field(client):
    cases = [
        ({"classify": {"method": "quantile", "k": 12}}, "classify.k"),
        ({"level": "admin_1"}, "level"),
        ({"variable": {"source": "natural_earth", "id": "GDP_PPP"}}, "variable.id"),
        ({"ramp": "Viridis"}, "ramp"),
    ]
    for override, field in cases:
        response = client.post("/map", json={**copy.deepcopy(RAW), **override})
        assert response.status_code == 422, override
        assert response.json()["field"] == field


def test_unknown_region_is_422(client):
    response = client.post("/map", json={**copy.deepcopy(RAW), "region": "atlantis"})
    assert response.status_code == 422
    assert response.json()["field"] == "region"


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
