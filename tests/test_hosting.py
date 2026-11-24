import copy

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mapsvc import api as api_module
from mapsvc import harvest as H
from mapsvc import hosting
from mapsvc.api import app
from tests.test_determinism import RAW, _features

ENVS = ("MAPSVC_CORS_ORIGINS", "MAPSVC_DESCRIBE_PER_IP_HOUR", "MAPSVC_DESCRIBE_PER_DAY",
        "MAPSVC_MAP_PER_IP_MINUTE", "MAPSVC_PROMPT_MAX_CHARS", "MAPSVC_DISABLE_EXPORT")


@pytest.fixture
def client(monkeypatch, tmp_path):
    for name in ENVS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MAPSVC_CACHE", str(tmp_path))
    monkeypatch.setenv("MAPSVC_OUTPUT", str(tmp_path / "out"))
    monkeypatch.setattr(H, "load_source", lambda level="admin_0": {"features": _features()})
    calls = []

    def fake_describe(prompt):
        calls.append(prompt)
        from mapsvc.manifest import validate
        raw = copy.deepcopy(RAW)
        return validate(raw), raw, "because"

    monkeypatch.setattr(api_module, "describe", fake_describe)
    clock = {"t": 1_000_000.0}
    monkeypatch.setattr(hosting, "_now", lambda: clock["t"])
    hosting.reset()
    c = TestClient(app)
    c.calls, c.clock = calls, clock
    return c


def test_healthz(client):
    r = client.get("/healthz")
    assert r.status_code == 200 and r.json() == {"ok": True}


def test_index_uses_relative_assets(client):
    html = client.get("/").text
    assert 'href="static/app.css"' in html
    assert html.index("static/config.js") < html.index("static/app.js")
    assert client.get("/static/config.js").text.startswith("window.MAPSVC_API_BASE")


def _cors_app(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("MAPSVC_CORS_ORIGINS", raising=False)
    else:
        monkeypatch.setenv("MAPSVC_CORS_ORIGINS", value)
    a = FastAPI()

    @a.post("/x")
    def x():
        return {}

    hosting.apply_cors(a)
    return TestClient(a)


def test_cors_off_by_default(monkeypatch):
    c = _cors_app(monkeypatch, None)
    r = c.options("/x", headers={"Origin": "https://a.example",
                                 "Access-Control-Request-Method": "POST"})
    assert "access-control-allow-origin" not in r.headers


def test_cors_allows_only_listed_origins(monkeypatch):
    c = _cors_app(monkeypatch, "https://a.example, https://b.example")
    ok = c.options("/x", headers={"Origin": "https://b.example",
                                  "Access-Control-Request-Method": "POST",
                                  "Access-Control-Request-Headers": "Content-Type"})
    assert ok.headers["access-control-allow-origin"] == "https://b.example"
    assert "POST" in ok.headers["access-control-allow-methods"]
    bad = c.options("/x", headers={"Origin": "https://evil.example",
                                   "Access-Control-Request-Method": "POST"})
    assert "access-control-allow-origin" not in bad.headers


def test_no_limits_when_unset(client):
    for _ in range(40):
        assert client.post("/describe", json={"prompt": "x"}).status_code == 200


def test_describe_per_ip_limit(client, monkeypatch):
    monkeypatch.setenv("MAPSVC_DESCRIBE_PER_IP_HOUR", "2")
    h = {"X-Real-IP": "1.1.1.1"}
    assert client.post("/describe", json={"prompt": "a"}, headers=h).status_code == 200
    client.clock["t"] += 600
    assert client.post("/describe", json={"prompt": "a"}, headers=h).status_code == 200
    r = client.post("/describe", json={"prompt": "a"}, headers=h)
    assert r.status_code == 429
    assert r.json()["field"] == "" and "try again" in r.json()["error"]
    assert int(r.headers["retry-after"]) == 3000   # first hit frees in 3600-600s
    assert len(client.calls) == 2                      # agent never called when refused
    other = client.post("/describe", json={"prompt": "a"}, headers={"X-Real-IP": "2.2.2.2"})
    assert other.status_code == 200
    client.clock["t"] += 3000                          # first hit has aged out
    assert client.post("/describe", json={"prompt": "a"}, headers=h).status_code == 200


def test_describe_global_daily_limit(client, monkeypatch):
    monkeypatch.setenv("MAPSVC_DESCRIBE_PER_DAY", "3")
    for i in range(3):
        assert client.post("/describe", json={"prompt": "a"},
                           headers={"X-Real-IP": f"9.9.9.{i}"}).status_code == 200
    r = client.post("/describe", json={"prompt": "a"}, headers={"X-Real-IP": "9.9.9.77"})
    assert r.status_code == 429 and "daily" in r.json()["error"]
    assert int(r.headers["retry-after"]) > 0
    assert len(client.calls) == 3
    client.clock["t"] += 86401
    assert client.post("/describe", json={"prompt": "a"}).status_code == 200


def test_refused_request_is_not_charged_to_open_cap(client, monkeypatch):
    monkeypatch.setenv("MAPSVC_DESCRIBE_PER_IP_HOUR", "1")
    monkeypatch.setenv("MAPSVC_DESCRIBE_PER_DAY", "2")
    h = {"X-Real-IP": "1.1.1.1"}
    client.post("/describe", json={"prompt": "a"}, headers=h)
    assert client.post("/describe", json={"prompt": "a"}, headers=h).status_code == 429
    # the day counter holds one hit, so another client still has room for one
    assert client.post("/describe", json={"prompt": "a"},
                       headers={"X-Real-IP": "2.2.2.2"}).status_code == 200


def test_prompt_export_shares_describe_budget(client, monkeypatch):
    monkeypatch.setenv("MAPSVC_DESCRIBE_PER_DAY", "1")
    assert client.post("/export", json={"prompt": "a"}).status_code == 200
    assert client.post("/describe", json={"prompt": "a"}).status_code == 429
    # an export of a given manifest never reaches the agent, so it is not counted
    assert client.post("/export", json=copy.deepcopy(RAW)).status_code == 200


def test_map_per_ip_limit(client, monkeypatch):
    monkeypatch.setenv("MAPSVC_MAP_PER_IP_MINUTE", "2")
    h = {"X-Real-IP": "5.5.5.5"}
    assert client.post("/map", json=copy.deepcopy(RAW), headers=h).status_code == 200
    assert client.post("/map", json=copy.deepcopy(RAW), headers=h).status_code == 200
    r = client.post("/map", json=copy.deepcopy(RAW), headers=h)
    assert r.status_code == 429 and 1 <= int(r.headers["retry-after"]) <= 60
    client.clock["t"] += 61
    assert client.post("/map", json=copy.deepcopy(RAW), headers=h).status_code == 200


def _request(headers, host="10.0.0.1"):
    from starlette.requests import Request
    scope = {"type": "http", "headers": [(k.lower().encode(), v.encode())
                                         for k, v in headers.items()],
             "client": (host, 1)}
    return Request(scope)


def test_client_identity():
    assert hosting.client_id(_request({"X-Real-IP": "1.2.3.4",
                                       "X-Forwarded-For": "6.6.6.6, 7.7.7.7"})) == "1.2.3.4"
    # leftmost is client-supplied (spoofable); rightmost was appended by our proxy
    assert hosting.client_id(_request({"X-Forwarded-For": "6.6.6.6, 7.7.7.7"})) == "7.7.7.7"
    assert hosting.client_id(_request({})) == "10.0.0.1"


def test_spoofed_leftmost_xff_does_not_evade_limit(client, monkeypatch):
    monkeypatch.setenv("MAPSVC_MAP_PER_IP_MINUTE", "1")
    h = lambda spoof: {"X-Forwarded-For": f"{spoof}, 8.8.8.8"}
    assert client.post("/map", json=copy.deepcopy(RAW), headers=h("1.1.1.1")).status_code == 200
    assert client.post("/map", json=copy.deepcopy(RAW), headers=h("2.2.2.2")).status_code == 429


def test_prompt_length_cap(client, monkeypatch):
    assert client.post("/describe", json={"prompt": "x" * 5000}).status_code == 200
    monkeypatch.setenv("MAPSVC_PROMPT_MAX_CHARS", "1000")
    r = client.post("/describe", json={"prompt": "x" * 1001})
    assert r.status_code == 422 and r.json()["field"] == "prompt"
    assert client.post("/export", json={"prompt": "x" * 1001}).status_code == 422
    assert client.post("/describe", json={"prompt": "x" * 1000}).status_code == 200
    assert len(client.calls) == 2


def test_too_long_prompt_does_not_spend_quota(client, monkeypatch):
    monkeypatch.setenv("MAPSVC_PROMPT_MAX_CHARS", "10")
    monkeypatch.setenv("MAPSVC_DESCRIBE_PER_DAY", "1")
    assert client.post("/describe", json={"prompt": "x" * 11}).status_code == 422
    assert client.post("/describe", json={"prompt": "ok"}).status_code == 200


def test_export_can_be_disabled(client, monkeypatch):
    assert client.post("/export", json=copy.deepcopy(RAW)).status_code == 200
    monkeypatch.setenv("MAPSVC_DISABLE_EXPORT", "1")
    r = client.post("/export", json=copy.deepcopy(RAW))
    assert r.status_code == 404 and "disabled" in r.json()["error"]
