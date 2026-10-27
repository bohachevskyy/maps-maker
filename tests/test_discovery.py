"""Offline proofs for general discovery, region and freshness mechanisms."""

import pytest

from mapsvc import agent
from mapsvc.manifest import ManifestError, validate
from mapsvc.overlays import OverlayError, overpass, web
from mapsvc.statistics import regions


def _taginfo_stub(monkeypatch):
    """Shaped like taginfo's real answers, measured 2026-10-26."""
    keyword = {
        "nuclear": [("power", "plant"), ("plant:source", "nuclear"),
                    ("man_made", "power_nuclear"), ("generator:source", "")],
        "power": [("power", "plant"), ("power", "generator"), ("frequency", "")],
        "plant": [("power", "plant"), ("plant:source", "nuclear"),
                  ("landuse", "plantation")],
    }
    uses = {("power", "plant"): (165277, "An industrial facility where power is produced."),
            ("plant:source", "nuclear"): (281, "Nuclear power plant"),
            ("power", "generator"): (900000, "A single power generator"),
            ("landuse", "plantation"): (60000, "Planted crops")}
    calls = []

    def response(path, params):
        calls.append((path, dict(params)))
        if path == "search/by_keyword":
            return {"data": [{"key": k, "value": v}
                             for k, v in keyword.get(params["query"], [])]}
        if path == "key/values":
            count, text = uses.get((params["key"], params["query"]), (0, ""))
            return {"data": [{"value": params["query"], "count": count,
                              "description": text}] if count else []}
        if path == "tag/combinations" and (params["key"], params["value"]) == ("power", "plant"):
            return {"data": [
                {"other_key": "plant:output:electricity", "other_value": "",
                 "together_count": 126277, "from_fraction": 0.98},
                {"other_key": "plant:source", "other_value": "nuclear",
                 "together_count": 281, "from_fraction": 0.01},
                {"other_key": "name", "other_value": "", "from_fraction": 0.9}]}
        return {"data": []}

    monkeypatch.setattr(overpass, "_taginfo_json", response)
    return calls


def test_taginfo_discovery_splits_the_phrase_and_returns_counts_and_hints(monkeypatch):
    calls = _taginfo_stub(monkeypatch)
    found = overpass.search("nuclear power plants")
    # taginfo's by_keyword matches one word at a time: each word was asked alone
    asked = {p["query"] for path, p in calls if path == "search/by_keyword"}
    assert asked == {"nuclear", "power", "plant"}
    ids = [c.id for c in found]
    assert ids[:2] == ["power=plant", "plant:source=nuclear"]
    assert "landuse=plantation" not in ids          # only one of the words
    plant = found[0]
    assert plant.count == 165277 and "165,277 uses" in plant.as_prompt_line()
    assert "industrial facility" in plant.subtitle
    assert "plant:source=nuclear" in plant.subtitle                  # qualifier
    assert "plant:output:electricity (attribute)" in plant.subtitle  # size attribute
    assert "name (attribute)" not in plant.subtitle


def test_taginfo_outage_means_no_candidates_not_an_exception(monkeypatch):
    def down(path, params):
        raise overpass.fetch.FetchError("timeout")
    monkeypatch.setattr(overpass, "_taginfo_json", down)
    assert overpass.search("lighthouses") == []


def test_overpass_is_a_searchable_overlay_source():
    from mapsvc import overlays
    assert overlays.capabilities("overpass").searchable


def test_agent_gets_taginfo_candidates_then_picks_one(monkeypatch, tmp_path):
    from tests.test_agent import FakeClient, reply, GOOD
    _taginfo_stub(monkeypatch)
    first = {**GOOD, "overlays": [{"source": "overpass", "id": "",
                                    "search_query": "nuclear power plants"}]}
    second = {**GOOD, "overlays": [{"source": "overpass", "id": "power=plant;plant:source=nuclear",
                                     "search_query": ""}]}
    client = FakeClient([reply(first), reply(second)])
    monkeypatch.setattr(agent, "_client", lambda: client)
    monkeypatch.setenv("MAPSVC_CACHE", str(tmp_path))
    monkeypatch.setenv("MAPSVC_LLM", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    monkeypatch.setattr(agent, "_preflight", lambda manifest: None)
    manifest, _, _ = agent.describe("nuclear power plants in France")
    turn = client.responses.calls[1]["input"][-1]["content"]
    assert "power=plant: power=plant (165,277 uses)" in turn
    assert "plant:output:electricity" in turn
    assert manifest.overlays[0][0] == "overpass"
    assert "plant:source=nuclear" in manifest.overlays[0][1]


def test_settlement_filter_without_city_is_repairably_rejected():
    with pytest.raises(OverlayError, match="must include city"):
        overpass.parse("place=town|village|hamlet")
    assert overpass.parse("place=city|town|village|hamlet") == \
        "node:place=city|town|village|hamlet"


def test_named_region_replaces_agent_country_list_and_documents_m49():
    raw = {"within": ["FRA"], "bbox": [], "level": "admin_0"}
    agent._expand_region(raw, "Population across Asia")
    assert set(raw["within"]) == set(regions.get("asia").members)
    assert "RUS" not in raw["within"] and "TUR" in raw["within"]
    assert {"TWN", "HKG", "MAC"} <= set(raw["within"])
    assert "Russia is Europe" in regions.get("asia").convention
    assert "Russia counted in Europe" in regions.get("asia").footnote


def test_region_expansion_is_conservative():
    def expand(prompt, **raw):
        raw = {"within": ["UKR"], "level": "admin_0", **raw}
        agent._expand_region(raw, prompt)
        return raw["within"]
    # the longest name wins: Central Asia is not all of Asia
    assert set(expand("GDP of Central Asia")) == set(regions.get("central asia").members)
    # an exclusion is the agent's own list
    assert expand("Europe except Russia") == ["UKR"]
    # sub-national maps and unit codes are never region lists
    assert expand("oblasts in Europe", level="admin_1") == ["UKR"]
    assert expand("Volyn raions in Europe", within=["UA-07"]) == ["UA-07"]
    # no region named: untouched
    assert expand("GDP of Ukraine") == ["UKR"]
    assert "TWN" in expand("population in East Asia", within=[])
    assert "MEX" in expand("North America population")


def test_region_table_keeps_separately_coded_territories():
    assert {"GUF", "FLK"} <= set(regions.get("south america").members)
    assert "RUS" in regions.get("europe").members
    assert "TUR" in regions.get("asia").members and "TUR" not in regions.get("europe").members
    assert set(regions.get("eu27").members) == set(regions.get("european union").members)
    assert len(regions.get("eu27").members) == 27
    assert regions.get("eu27").name == "EU27"
    # each country is in exactly one M49 sub-region
    seen = {}
    for name in regions._SUBREGIONS:
        for code in regions.get(name).members:
            assert seen.setdefault(code, name) == name, code


def test_a_named_region_is_recognised_from_its_members():
    assert regions.identify(regions.get("europe").members).name == "Europe"
    assert regions.identify(["FRA", "DEU"]) is None


def test_region_members_missing_from_the_basemap_are_a_stated_gap_not_an_error(
        monkeypatch):
    from mapsvc import harvest
    manifest = validate({"level": "admin_0", "within": list(regions.get("asia").members),
                         "variable": {"source": "natural_earth", "id": "POP_EST"},
                         "basemap": {"source": "natural_earth"}})
    boundaries = type("B", (), {"units": [{"id": "CHN", "keys": {}}]})()
    missing = harvest._require_named_units(manifest, boundaries)
    assert "HKG" in missing and "CHN" not in missing
    ad_hoc = validate({"level": "admin_0", "within": ["CHN", "HKG"],
                       "variable": {"source": "natural_earth", "id": "POP_EST"},
                       "basemap": {"source": "natural_earth"}})
    with pytest.raises(harvest.HarvestError, match="HKG"):
        harvest._require_named_units(ad_hoc, boundaries)


def test_official_population_source_is_preferred_over_wikidata():
    with pytest.raises(ManifestError, match="official statistics source statcan") as caught:
        validate({"level": "admin_1", "within": ["CAN"],
                  "variable": {"source": "wikidata", "id": "population"}})
    assert caught.value.field == "variable.source"
    # no official table covers Brazil: Wikidata is the last resort and is accepted
    validate({"level": "admin_1", "within": ["BRA"],
              "variable": {"source": "wikidata", "id": "population"}})


def test_mixed_vintage_spread_is_recorded_and_rejected_only_with_an_alternative():
    from mapsvc import harvest

    broad = validate({"level": "admin_0", "within": list(regions.get("europe").members),
                      "variable": {"source": "natural_earth", "id": "GDP_MD"},
                      "basemap": {"source": "natural_earth", "detail": "simplified"}})
    provenance = {}
    harvest._check_year_spread(broad, provenance, {2015, 2024})
    assert provenance["year_spread"] == 9 and provenance["year_range"] == [2015, 2024]
    problem = harvest.stale_series(broad, provenance)
    assert "spans 9 years" in problem and "OWID GDP" in problem

    provenance = {}
    harvest._check_year_spread(broad, provenance, {2021, 2024})
    assert harvest.stale_series(broad, provenance) is None      # 3 years: fine

    last_resort = validate({"level": "admin_1", "within": ["BRA"],
                            "variable": {"source": "wikidata", "id": "population"}})
    provenance = {}
    harvest._check_year_spread(last_resort, provenance, {2010, 2024})
    assert provenance["year_spread"] == 14
    assert harvest.stale_series(last_resort, provenance) is None  # no alternative


def test_a_stale_series_reaches_the_agents_repair_turn(monkeypatch, tmp_path):
    from mapsvc import harvest
    from tests.test_agent import FakeClient, reply
    stale = {"bbox": [], "within": ["DEU", "FRA", "ITA", "ESP"], "level": "admin_0",
             "variable": {"source": "natural_earth", "id": "GDP_MD"}}
    fresh = {**stale, "variable": {"source": "natural_earth", "id": "POP_EST"}}
    client = FakeClient([reply(stale), reply(fresh)])
    monkeypatch.setattr(agent, "_client", lambda: client)
    monkeypatch.setenv("MAPSVC_CACHE", str(tmp_path))
    monkeypatch.setenv("MAPSVC_LLM", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "k")

    def fake_harvest(manifest):
        spread = 9 if manifest.variable_id == "GDP_MD" else 0
        return type("R", (), {"provenance": {"year_spread": spread,
                                              "year_range": [2015, 2024]}})()
    monkeypatch.setattr(harvest, "harvest", fake_harvest)
    monkeypatch.setattr(harvest, "validate_window", lambda manifest: None)
    manifest, _, _ = agent.describe("GDP of big EU countries")
    followup = client.responses.calls[1]["input"][-1]["content"]
    assert "rejected" in followup and "spans 9 years" in followup
    assert manifest.variable_id == "POP_EST"


def test_settlement_omitting_city_is_a_repairable_agent_error(monkeypatch, tmp_path):
    from tests.test_agent import FakeClient, reply, GOOD
    towns = {**GOOD, "overlays": [{"source": "overpass", "id": "place=town|village",
                                    "search_query": ""}]}
    fixed = {**GOOD, "overlays": [{"source": "overpass",
                                    "id": "place=city|town|village|hamlet",
                                    "search_query": ""}]}
    client = FakeClient([reply(towns), reply(fixed)])
    monkeypatch.setattr(agent, "_client", lambda: client)
    monkeypatch.setenv("MAPSVC_CACHE", str(tmp_path))
    monkeypatch.setenv("MAPSVC_LLM", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    monkeypatch.setattr(agent, "_preflight", lambda manifest: None)
    manifest, _, _ = agent.describe("towns and villages of Kyiv oblast")
    followup = client.responses.calls[1]["input"][-1]["content"]
    assert "must include city" in followup
    assert manifest.overlays[0][1].endswith("place=city|town|village|hamlet")


def test_ibtracs_general_entry_requires_valid_basin_and_uses_basin_file(monkeypatch, tmp_path):
    all_id = web._IBTRACS_ALL + "#basin=WP;season=2024"
    assert web.parse(all_id) == all_id
    for bad in ("#basin=XX;season=2024", "#season=2024", "#basin=WP"):
        with pytest.raises(OverlayError):
            web.parse(web._IBTRACS_ALL + bad)
    assert web.parse(web._IBTRACS_ALL + "#basin=wp;season=2024").endswith("basin=WP;season=2024")

    calls = []
    monkeypatch.setattr(web.fetch, "path", lambda *parts: tmp_path.joinpath(*parts))
    monkeypatch.setattr(web, "_download", lambda url, limit: calls.append(url) or b"sample")
    web.fetch_bytes(all_id)
    web.fetch_bytes(web._IBTRACS_ALL + "#basin=WP;season=2023")
    web.fetch_bytes(web._IBTRACS_ALL + "#basin=SP;season=2023")
    assert calls == [web._IBTRACS + "ibtracs.WP.list.v04r01.csv",
                     web._IBTRACS + "ibtracs.SP.list.v04r01.csv"]


def test_ibtracs_layer_names_the_requested_basin():
    from pathlib import Path
    payload = (Path(__file__).parent / "fixtures" / "ibtracs_na_sample.csv").read_bytes()
    window = web.Window((-180, -90, 180, 90), None, (-180, -90, 180, 90))
    layer = web.layer_from_bytes(
        payload, web.parse(web._IBTRACS_ALL + "#basin=NA;season=2024"), window)
    assert "North Atlantic" in layer["title"]
    assert "North Atlantic basin; season 2024" in layer["provenance"]["vintage"]


def test_catalogue_search_offers_one_general_cyclone_entry():
    top = web.search("typhoons in the western pacific")[0]
    assert top.id == web._IBTRACS_ALL
    line = top.as_prompt_line()
    assert "basin=" in line and "NA|EP|WP|NI|SI|SP|SA" in line
