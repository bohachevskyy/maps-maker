"""Research -> validate -> register, with the web and the model faked."""

import types

from mapsvc.discovery import http, join, pipeline
from mapsvc.discovery.store import Store
from tests.test_research_agent import PLAN, PROPOSE, REPORT, SELECT, FakeLLM

CSV = b"Country Code,Country Name,2023\nBRA,Brazil,1761\nARG,Argentina,591\nCHL,Chile,1522\n"


def _discover(tmp_path, monkeypatch, llm, *, ratio=1.0, payload=CSV):
    monkeypatch.setattr(http, "get", lambda url, **kw: types.SimpleNamespace(content=payload))
    monkeypatch.setattr(join, "coverage",
                        lambda values, ns, level, scope: {"matched": 3, "expected": 3, "ratio": ratio})
    store = Store(tmp_path / "d.sqlite")
    hits = [{"title": "Precipitation", "url": "https://data.worldbank.org/x", "snippet": "mm"}]
    outcome = pipeline.discover(
        "Average annual rainfall by country", "no candidate", ask=llm,
        search=lambda q: hits,
        fetch=lambda url: {"kind": "page", "links": ["https://api.worldbank.org/x.csv"]},
        probe_fn=lambda url: REPORT, store=store)
    return outcome, store


def test_a_valid_find_is_registered_and_searchable(tmp_path, monkeypatch):
    outcome, store = _discover(tmp_path, monkeypatch, FakeLLM(PLAN, SELECT, PROPOSE))
    assert outcome.refusal is None
    rec = outcome.record
    assert rec["url"] == "https://api.worldbank.org/x.csv" and rec["publisher"] == "World Bank"
    assert rec["status"] == "active" and rec["coverage"]["ratio"] == 1.0
    assert [r["id"] for r in store.search_any("rainfall precipitation")] == [rec["id"]]


def test_thin_coverage_is_refused_and_remembered(tmp_path, monkeypatch):
    llm = FakeLLM(PLAN, SELECT, PROPOSE)
    outcome, store = _discover(tmp_path, monkeypatch, llm, ratio=0.2)
    assert outcome.record is None and "failed validation" in outcome.refusal
    assert store.search_any("precipitation") == []
    again = pipeline.discover("Average annual rainfall by country", "x", ask=llm,
                              search=None, fetch=None, store=store)
    assert again.record is None and "searched recently" in again.refusal
    assert len(llm.calls) == 3               # the second call never reached the model


def test_a_research_refusal_is_remembered_too(tmp_path, monkeypatch):
    plan = dict(PLAN, give_up=True, give_up_reason="not published below national level")
    outcome, store = _discover(tmp_path, monkeypatch, FakeLLM(plan))
    assert outcome.record is None
    assert store.recent_negative("average annual rainfall  BY country")


def test_the_main_agent_is_told_how_to_use_each_kind():
    choro = pipeline.prompt_for_main_agent(
        {"id": "disc_1", "geometry_kind": "choropleth", "level": "admin_0", "url": "u"})
    points = pipeline.prompt_for_main_agent(
        {"id": "disc_2", "geometry_kind": "points", "url": "https://e.org/p.csv"})
    assert '"source": "discovered", "id": "disc_1"' in choro
    assert '"source": "web", "id": "https://e.org/p.csv"' in points


def test_a_discovered_points_file_is_drawn_with_its_validated_filter(tmp_path, monkeypatch):
    from mapsvc.overlays import web
    from mapsvc.overlays import Window
    monkeypatch.setenv("MAPSVC_DISCOVERED_DB", str(tmp_path / "d.sqlite"))
    url = "https://data.example.org/meteorites.csv"
    Store().add_or_update({
        "id": "disc_aaaabbbbccccdddd", "url": url, "title": "Meteorite Landings",
        "publisher": "NASA", "licence": "public domain", "geometry_kind": "points",
        "key_namespace": "coords", "level": "points", "status": "active", "format": "csv",
        "roles": {"lon": "reclong", "lat": "reclat", "name": "name",
                  "filter": [{"column": "fall", "op": "eq", "value": "Fell"}]}})
    csv = (b"name,fall,reclong,reclat\nAachen,Fell,6.08,50.77\nAbee,Found,-113,54.2\n"
           b"Acapulco,Fell,-99.9,16.88\n")
    layer = web.layer_from_bytes(csv, url, Window(None, None, (-180.0, -90.0, 180.0, 90.0)))
    names = sorted(str(f) for f in layer.get("features", layer.get("places", [])))
    assert "Abee" not in " ".join(names) and len(names) == 2
    assert "NASA" in layer["provenance"]["attribution"]
