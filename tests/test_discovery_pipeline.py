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


def test_thin_coverage_is_refused_after_one_repair_and_remembered(tmp_path, monkeypatch):
    llm = FakeLLM(PLAN, SELECT, PROPOSE, PROPOSE)
    outcome, store = _discover(tmp_path, monkeypatch, llm, ratio=0.2)
    assert outcome.record is None and "failed validation" in outcome.refusal
    assert store.search_any("precipitation") == []
    again = pipeline.discover("Average annual rainfall by country", "x", ask=llm,
                              search=None, fetch=None, store=store)
    assert again.record is None and "searched recently" in again.refusal
    assert [c[0] for c in llm.calls][-1] == "research_repair"
    assert len(llm.calls) == 4               # the second request never reached the model


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


def test_a_rejected_proposal_is_repaired_with_the_services_reasons(tmp_path, monkeypatch):
    """Two rows per country (an AGE dimension) is caught, and the agent adds a filter."""
    long = (b"Country Code,Country Name,AGE,2023\nBRA,Brazil,_T,10\nBRA,Brazil,Y0T17,4\n"
            b"ARG,Argentina,_T,8\nARG,Argentina,Y0T17,3\nCHL,Chile,_T,5\nCHL,Chile,Y0T17,2\n")
    report = dict(REPORT, columns=REPORT["columns"] + [{"name": "AGE", "inferred": [], "sample": ["_T"]}])
    roles = dict(PROPOSE["roles"], date="AGE")      # wrongly treats AGE as the date
    fixed = dict(PROPOSE, filters=[{"column": "AGE", "op": "eq", "value": "_T"}])
    llm = FakeLLM(PLAN, SELECT, dict(PROPOSE, roles=dict(PROPOSE["roles"])), fixed)
    monkeypatch.setattr(http, "get", lambda url, **kw: types.SimpleNamespace(content=long))
    monkeypatch.setattr(join, "coverage", lambda *a: {"matched": 3, "expected": 3, "ratio": 1.0})
    outcome = pipeline.discover(
        "rainfall", "none", ask=llm, search=lambda q: [{"title": "t", "url": "https://e.org/p", "snippet": ""}],
        fetch=lambda url: {"kind": "page", "links": ["https://e.org/d.csv"]},
        probe_fn=lambda url: report, store=Store(tmp_path / "d.sqlite"))
    validations = [t for t in outcome.trace if t["step"] == "validate"]
    assert not validations[0]["ok"] and "AGE (Y0T17, _T)" in validations[0]["problems"][0]
    assert outcome.record and outcome.record["roles"]["filter"][0]["value"] == "_T"


def test_start_and_end_columns_draw_as_segments_coloured_in_natural_order(tmp_path, monkeypatch):
    from mapsvc.overlays import Window, web
    monkeypatch.setenv("MAPSVC_DISCOVERED_DB", str(tmp_path / "d.sqlite"))
    url = "https://data.example.org/tornadoes.csv"
    Store().add_or_update({
        "id": "disc_1111222233334444", "url": url, "title": "Tornadoes", "publisher": "NOAA SPC",
        "licence": "public domain", "geometry_kind": "lines", "key_namespace": "coords",
        "level": "lines", "status": "active", "format": "csv",
        "roles": {"lon": "slon", "lat": "slat", "end_lon": "elon", "end_lat": "elat",
                  "category": "mag", "filter": [{"column": "date", "op": "ge", "value": "2011-04-25"}]}})
    csv = (b"date,mag,slon,slat,elon,elat\n2011-04-27,10,-88.1,34.1,-87.9,34.3\n"
           b"2011-04-27,2,-87.0,33.0,-86.8,33.2\n2011-03-01,1,-90,30,-89.9,30.1\n")
    layer = web.layer_from_bytes(csv, url, Window(None, None, (-180.0, -90.0, 180.0, 90.0)))
    assert layer["kind"] == "lines" and len(layer["features"]) == 2
    assert layer["features"][0]["coords"] == [[-88.1, 34.1], [-87.9, 34.3]]
    assert [c["value"] for c in layer["category"]["classes"]] == ["2", "10"]
