"""Web overlays reached through the catalogue: filters, IBTrACS storm tracks,
GeoNames rankings, GVP volcano types and zipped files. Offline: every file is
a small recorded sample, or built here."""

import io
import json
import pathlib
import zipfile

import pytest

from mapsvc import overlay_render, overlays
from mapsvc.manifest import ManifestError, validate
from mapsvc.models import HarvestResult
from mapsvc.overlays import web
from mapsvc.render import render

from tests.conftest import build_manifest

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
IBTRACS = web._IBTRACS + "ibtracs.NA.list.v04r01.csv"
WORLD = overlays.Window(None, None, (-180.0, -90.0, 180.0, 90.0))


def _raw(**overrides):
    raw = {"level": "admin_0", "bbox": [-180, -90, 180, 90],
           "basemap": {"source": "natural_earth", "detail": "simplified"}}
    raw.update(overrides)
    return raw


def _map(mini_geojson, layers):
    manifest = build_manifest(variable_source=None, variable_id=None,
                              overlays=tuple((l["source"], l["id"]) for l in layers))
    rows = [{"id": f["properties"]["ADM0_A3"], "geometry": f["geometry"], "value": None}
            for f in mini_geojson["features"]]
    result = HarvestResult(rows=rows, provenance={"source": "Natural Earth",
                                                  "scale": "50m", "vintage": "v5.1.1"},
                           dropped=[], overlays=list(layers))
    return render(manifest, result)


# ---------------------------------------------------------------- filters ---

def test_filters_are_canonicalised_and_checked_before_io():
    assert web.parse(IBTRACS + "#season=2024") == IBTRACS + "#season=2024"
    assert web.parse(web._GEONAMES + "# top=30 ") == web._GEONAMES + "#top=30"
    # A season is required: the archive is 1851 to now, thousands of storms.
    with pytest.raises(overlays.OverlayError, match="needs season"):
        web.parse(IBTRACS)
    with pytest.raises(overlays.OverlayError, match="needs top"):
        web.parse(web._GEONAMES)


@pytest.mark.parametrize("bad", [
    IBTRACS + "#season=1700",            # before the archive
    IBTRACS + "#season=2024;season=2023",
    IBTRACS + "#season=2024;basin=1",    # not a filter this entry declares
    IBTRACS + "#season=twenty",
    "https://data.example.org/x.csv#season=2024",   # only top on unknown URLs
    "https://data.example.org/x.csv#top=0",
    "https://data.example.org/x.csv#top=5000",
    "https://data.example.org/x.csv#where=1;drop table",
])
def test_filters_outside_the_grammar_are_refused(bad):
    with pytest.raises(ManifestError):
        validate(_raw(overlays=[{"source": "web", "id": bad}]))


def test_an_unknown_url_may_be_ranked():
    url = "https://data.example.org/x.csv"
    assert web.parse(url + "#top=5") == url + "#top=5"
    assert validate(_raw(overlays=[{"source": "web", "id": url + "#top=5"}])).overlays == (
        ("web", url + "#top=5"),)


def test_filters_share_one_cached_download(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(web.fetch, "path", lambda *parts: tmp_path.joinpath(*parts))
    monkeypatch.setattr(web, "_download", lambda url, limit: calls.append((url, limit)) or b"x")
    web.fetch_bytes(IBTRACS + "#season=2024")
    web.fetch_bytes(IBTRACS + "#season=2005")
    # The archive is 57 MB: a catalogue entry may exceed the arbitrary-URL cap.
    assert calls == [(IBTRACS, 100 * 1024 * 1024)]


def test_search_finds_the_new_entries():
    assert web.search("atlantic hurricanes tropical storms")[0].id == IBTRACS
    assert web.search("volcanoes active holocene")[0].id == web._GVP
    assert web.search("largest cities by population")[0].id == web._GEONAMES
    line = web.search("hurricane tracks")[0].as_prompt_line()
    assert "season=<1851-" in line and "Requires season" in line


# ---------------------------------------------------------------- ibtracs ---

def _storms(season=2024):
    payload = (FIXTURES / "ibtracs_na_sample.csv").read_bytes()
    return web.layer_from_bytes(payload, web.parse(f"{IBTRACS}#season={season}"), WORLD)


def test_ibtracs_season_is_one_line_per_storm_with_category_along_it():
    layer = _storms()
    assert layer["kind"] == "lines" and layer["along"]
    # Milton and Chris; the unnamed system never became a tropical cyclone.
    assert [f["name"] for f in layer["features"]] == ["Chris", "Milton"]
    assert layer["excluded"] == [{"count": 1, "reason": "never a tropical or "
                                  "subtropical cyclone (disturbances, invests)"}]
    milton = layer["features"][1]
    assert milton["peak"] == 5 and milton["peak_wind"] == 155.0
    assert len(milton["classes"]) == len(milton["coords"]) - 1
    assert {"td", "ts", "cat1", "cat5"} <= set(milton["classes"])
    assert layer["stats"] == {"count": 2, "hurricanes": 1, "major_hurricanes": 1}
    assert layer["title"].endswith("2024")
    assert "public domain" in layer["provenance"]["license"]


def test_ibtracs_season_without_storms_is_an_error():
    with pytest.raises(overlays.OverlayError, match="no storms in season 1990"):
        _storms(1990)


def test_storm_tracks_render_by_category_and_name_hurricanes(mini_geojson):
    layer = _storms()
    for f in layer["features"]:          # into the fixture window
        f["coords"] = [[lon + 95, lat + 25] for lon, lat in f["coords"]]
        f["peak_at"] = [f["peak_at"][0] + 95, f["peak_at"][1] + 25]
    svg = _map(mini_geojson, [layer])
    assert f'stroke="{overlay_render.SSHS["cat5"]}"' in svg
    assert "Category 5" in svg and "Saffir-Simpson category" in svg
    assert ">Milton<" in svg and ">Chris<" in svg       # every named storm
    assert "2 storms drawn" in svg
    assert svg == _map(mini_geojson, [layer])


# --------------------------------------------------------------- rankings ---

CITIES = [
    # geonameid, name, lat, lon, feature code, country, population
    ("1", "Moscow", 55.75, 37.62, "PPLC", "RU", 10381222),
    ("2", "London", 51.51, -0.13, "PPLC", "GB", 8961989),
    ("3", "Ankara", 39.92, 32.85, "PPLC", "TR", 3517182),
    ("4", "Novosibirsk", 55.04, 82.93, "PPLA", "RU", 1612833),
    ("5", "Pest", 47.5, 19.08, "PPLX", "HU", 1001748),
    ("6", "Paris", 48.85, 2.35, "PPLC", "FR", 2138551),
    ("7", "Lyon", 45.75, 4.85, "PPLA", "FR", 522969),
]


def _geonames_zip(rows=CITIES) -> bytes:
    lines = []
    for gid, name, lat, lon, code, cc, pop in rows:
        cells = [gid, name, name, "", str(lat), str(lon), "P", code, cc, "", "", "",
                 "", "", str(pop), "", "100", "Europe/X", "2026-01-01"]
        lines.append("\t".join(cells))
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("cities15000.txt", "\n".join(lines) + "\n")
    return buffer.getvalue()


@pytest.fixture
def iso2(monkeypatch):
    from mapsvc.cartography import iso

    table = {"RUS": "RU", "GBR": "GB", "FRA": "FR", "HUN": "HU", "TUR": "TR"}
    monkeypatch.setattr(iso, "to_alpha2", lambda c: c if len(c) == 2 else table.get(c))


def test_top_n_is_ranked_inside_the_window_and_the_within_countries(iso2):
    europe = overlays.Window((-25.0, 34.0, 60.0, 72.0), ("GBR", "FRA", "RUS", "HUN"),
                             (-25.0, 34.0, 60.0, 72.0))
    layer = web.layer_from_bytes(_geonames_zip(), web._GEONAMES + "#top=3", europe)
    # Ankara is in the box but not in `within`; Novosibirsk is in Russia but
    # east of the box; Pest is a section of Budapest, not a city.
    assert [(f["name"], f["rank"]) for f in layer["features"]] == [
        ("Moscow", 1), ("London", 2), ("Paris", 3)]
    reasons = {e["reason"]: e["count"] for e in layer["excluded"]}
    assert reasons["outside the map window"] == 1
    assert reasons["in countries not on the map"] == 1
    assert reasons["smaller than the 3 largest"] == 1
    assert reasons["districts of a city, or historical, abandoned or destroyed places"] == 1
    assert "caveat: Moscow is a 2010 census count" in layer["encoding"]
    assert layer["title"] == "The 3 largest cities by population"
    assert "city proper" in layer["summary"] and "not the metropolitan area" in layer["encoding"]
    assert layer["provenance"]["license"] == "CC BY 4.0"


def test_ranking_needs_a_size_column():
    csv_bytes = b"name,lat,lon\nA,50,30\nB,51,31\n"
    with pytest.raises(overlays.OverlayError, match="no numeric size"):
        web.layer_from_bytes(csv_bytes, "https://data.example.org/x.csv#top=1", WORLD)


def test_ranked_points_render_every_label_with_its_value(mini_geojson, iso2):
    window = overlays.Window((-5.0, 35.0, 40.0, 60.0), None, (-5.0, 35.0, 40.0, 60.0))
    layer = web.layer_from_bytes(_geonames_zip(), web._GEONAMES + "#top=4", window)
    svg = _map(mini_geojson, [layer])
    for label in ("Moscow 10.4M", "London 9.0M", "Ankara 3.5M", "Paris 2.1M"):
        assert f">{label}</text>" in svg
    assert 'data-stat-count="4"' in svg and "data-unlabelled" not in svg
    assert "Population, city proper (circle area)" in svg
    assert svg == _map(mini_geojson, [layer])


# ------------------------------------------------------------------- zips ---

def test_a_zip_must_hold_exactly_one_data_file():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("a.csv", "lat,lon\n1,2\n")
        archive.writestr("b.csv", "lat,lon\n1,2\n")
    with pytest.raises(overlays.OverlayError, match="2 data files"):
        web.layer_from_bytes(buffer.getvalue(), "https://data.example.org/x.zip", WORLD)


def test_a_zip_bomb_is_refused_before_it_is_read():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("a.csv", "lat,lon\n" + "0" * (20 * 1024 * 1024))
    with pytest.raises(overlays.OverlayError, match="unpacks to"):
        web.layer_from_bytes(buffer.getvalue(), "https://data.example.org/x.zip", WORLD)


def test_a_zipped_csv_is_read_like_a_csv():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("data/places.csv", "name,lat,lon,count\nA,50,30,1\nB,51,31,2\n")
    layer = web.layer_from_bytes(buffer.getvalue(), "https://data.example.org/x.zip", WORLD)
    assert layer["kind"] == "points" and len(layer["features"]) == 2


# -------------------------------------------------------------- volcanoes ---

def test_volcano_types_collapse_to_a_handful_of_ordered_classes():
    types = ["Stratovolcano", "Shield", "Complex", "Volcanic field", "Maar(s)",
             "Lava dome(s)", "Caldera", "Fissure vent(s)", "None"]
    features = [{"type": "Feature", "geometry": {"type": "Point", "coordinates": [i, i]},
                 "properties": {"Volcano_Name": f"V{i}", "Primary_Volcano_Type": t}}
                for i, t in enumerate(types)]
    payload = json.dumps({"type": "FeatureCollection", "features": features}).encode()
    layer = web.layer_from_bytes(payload, web._GVP, WORLD)
    assert layer["kind"] == "points" and layer["stats"]["count"] == 9
    assert [c["value"] for c in layer["category"]["classes"]] == [
        "strato", "shield", "caldera", "field", "fissure", "small", "other"]
    assert [f["category"] for f in layer["features"]][:3] == ["strato", "shield", "strato"]
    assert "Venzke" in layer["provenance"]["attribution"]
    assert layer["label_top"] == 15      # the notable few, ranked from the record itself


def test_points_round_the_globe_centre_on_their_emptiest_seam():
    # Many points all round the Pacific rim and the Atlantic ridge, none in
    # central Asia: no mean centre, but an obvious place for the seam.
    lons = [v for v in range(-180, 180, 2) if not 40 <= v <= 80] * 3
    layer = {"kind": "points", "features": [{"lon": v, "lat": 0} for v in lons]}
    assert overlay_render.centre_lon([layer]) == -120.0
    # Evenly spread: no seam is better than another, so keep Greenwich.
    even = {"kind": "points", "features": [{"lon": v, "lat": 0}
                                           for v in range(-180, 180, 1)]}
    assert overlay_render.centre_lon([even]) is None


def test_the_agent_is_shown_the_filters_it_may_use():
    from mapsvc import agent

    section = agent._overlay_section()
    assert "#top=N" in section and "European Russia" in section
    offered = agent._overlay_candidates("web", "volcanoes holocene")
    assert web._GVP in offered and "Filters after '#'" in offered
    storms = agent._overlay_candidates("web", "atlantic hurricane tracks")
    assert "Requires season" in storms


def test_a_volcano_whose_record_reports_deaths_outranks_a_quiet_one():
    fields = {"text": "Geological_Summary", "year": "Last_Eruption_Year", "size": "Elevation"}
    deadly = {"Geological_Summary": "A catastrophic eruption killed 36,000; the tsunami "
                                    "devastated the coast.", "Last_Eruption_Year": 1883,
              "Elevation": 813}
    quiet = {"Geological_Summary": "A field of cones.", "Last_Eruption_Year": -3000,
             "Elevation": 813}
    assert web._notability(deadly, fields) > web._notability(quiet, fields) + 2
