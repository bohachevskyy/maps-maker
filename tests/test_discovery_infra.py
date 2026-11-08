"""Offline tests for the research-agent discovery infrastructure."""

import io
import json
import zipfile

import pytest

from mapsvc.cartography import Boundaries
from mapsvc.discovery import http, probe
from mapsvc.discovery.store import Store, source_id


class _Reply:
    def __init__(self, content=b"a,b\n1,2\n", *, content_type="text/csv"):
        self.content = io.BytesIO(content)
        self.status = 200
        self.headers = {"Content-Type": content_type, "Set-Cookie": "secret"}

    def read(self, amount=-1):
        return self.content.read(amount)

    def getcode(self):
        return self.status

    def geturl(self):
        return "https://data.example/file.csv"

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


class _Opener:
    def __init__(self, replies):
        self.replies = list(replies)

    def open(self, request, timeout):
        return self.replies.pop(0)


def _public_dns(*args, **kwargs):
    return [(2, 1, 6, "", ("93.184.216.34", 443))]


def test_http_records_replays_and_reports_miss(monkeypatch, tmp_path):
    monkeypatch.setenv("MAPSVC_CASSETTES", str(tmp_path))
    monkeypatch.setenv("MAPSVC_REPLAY", "record")
    monkeypatch.setattr(http.socket, "getaddrinfo", _public_dns)
    monkeypatch.setattr(http.urllib.request, "build_opener",
                        lambda *handlers: _Opener([_Reply()]))
    recorded = http.get("https://data.example/file.csv?z=2", params={"a": 1},
                        headers={"Authorization": "Bearer secret"}, expect="data")
    assert not recorded.from_replay
    metadata = json.loads(next(tmp_path.glob("*.json")).read_text())
    assert "secret" not in json.dumps(metadata)
    assert metadata["url"].endswith("?a=1&z=2")
    assert "Set-Cookie" not in metadata["headers"]

    monkeypatch.setenv("MAPSVC_REPLAY", "replay")
    replayed = http.get("https://data.example/file.csv", params=[("z", 2), ("a", 1)],
                        expect="data")
    assert replayed.from_replay and replayed.content == recorded.content
    with pytest.raises(http.ReplayMiss, match="replay miss.*other.csv"):
        http.get("https://data.example/other.csv")


def test_http_ssrf_size_and_html_guards(monkeypatch, tmp_path):
    monkeypatch.setenv("MAPSVC_REPLAY", "off")
    with pytest.raises(http.SSRFError):
        http.get("file:///etc/passwd")
    with pytest.raises(http.SSRFError):
        http.get("http://127.0.0.1/data.csv")

    monkeypatch.setattr(http.socket, "getaddrinfo", _public_dns)
    monkeypatch.setattr(http.urllib.request, "build_opener",
                        lambda *handlers: _Opener([_Reply(b"12345")]))
    with pytest.raises(http.ResponseTooLarge):
        http.get("https://data.example/file.csv", max_bytes=4)

    monkeypatch.setenv("MAPSVC_REPLAY", "record")
    monkeypatch.setenv("MAPSVC_CASSETTES", str(tmp_path))
    monkeypatch.setattr(http.urllib.request, "build_opener", lambda *handlers: _Opener([
        _Reply(b"<!doctype html><title>error</title>", content_type="text/html")]))
    with pytest.raises(http.UnexpectedContent):
        http.get("https://data.example/file.csv", expect="data")
    assert list(tmp_path.iterdir()) == []


def test_store_crud_fts_filters_negatives_and_ordering(tmp_path):
    with Store(tmp_path / "found.sqlite") as store:
        first = store.add_or_update({
            "url": "https://example.org/z.csv", "title": "World population",
            "description": "Country population table", "publisher": "Example",
            "licence": "CC0", "format": "csv", "geometry_kind": "choropleth",
            "key_namespace": "ISO3", "level": "admin_0", "roles": {"key": "iso3"},
            "coverage": {"matched": 2, "expected": 2, "ratio": 1, "scope": "world"},
            "validation": {"ok": True}, "keywords": "people demographic",
            "query_text": "population by country",
        })
        second = store.add_or_update({
            "url": "https://example.org/a.csv", "title": "Population points",
            "description": "Cities", "publisher": "Example", "format": "csv",
            "geometry_kind": "points", "key_namespace": "coords", "level": "city",
            "keywords": "population",
        })
        assert first["id"] == source_id(first["url"])
        assert first["roles"] == {"key": "iso3"}
        assert [row["id"] for row in store.search("")] == sorted([first["id"], second["id"]])
        found = store.search("world population", key_namespace="ISO3")
        assert [row["id"] for row in found] == [first["id"]]
        assert store.mark_used(first["id"])["uses"] == 1
        rejected = store.set_status(first["id"], "rejected", "bad coverage")
        assert rejected["validation"]["status_reason"] == "bad coverage"
        assert store.search("population", status="rejected")[0]["id"] == first["id"]

        store.record_negative("  No   such DATA ", {"searched": 3})
        assert store.recent_negative("no such data")["report"] == {"searched": 3}


def _xlsx() -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("xl/workbook.xml", """
          <workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
           xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
           <sheets><sheet name="Data" sheetId="1" r:id="rId1"/></sheets></workbook>""")
        archive.writestr("xl/_rels/workbook.xml.rels", """
          <Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
           <Relationship Id="rId1" Target="worksheets/sheet1.xml"/></Relationships>""")
        archive.writestr("xl/worksheets/sheet1.xml", """
          <worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>
          <row r="1"><c r="A1" t="inlineStr"><is><t>ISO3</t></is></c><c r="B1" t="inlineStr"><is><t>value</t></is></c></row>
          <row r="2"><c r="A2" t="inlineStr"><is><t>FRA</t></is></c><c r="B2"><v>7</v></c></row>
          </sheetData></worksheet>""")
    return output.getvalue()


@pytest.mark.parametrize(("payload", "expected"), [
    (b"metadata only\nISO3,lat,lon,value\nFRA,48,2,10\nDEU,51,9,20\n", "csv"),
    (b"ISO3\tlat\tlon\tvalue\nFRA\t48\t2\t10\n", "tsv"),
    (json.dumps([{"ISO3": "FRA", "value": 1}]).encode(), "json"),
    (json.dumps({"type": "FeatureCollection", "features": [{"type": "Feature",
      "properties": {"name": "Paris"}, "geometry": {"type": "Point", "coordinates": [2, 48]}}]}).encode(), "geojson"),
    (_xlsx(), "xlsx"),
])
def test_probe_formats_and_role_inference(payload, expected):
    report = probe.probe_bytes(payload, url="https://example.org/data")
    assert report.format == expected and report.n_rows >= 1
    roles = {item["role"] for column in report.columns for item in column["inferred"]}
    if expected != "geojson":
        assert "numeric value" in roles
    if expected in {"csv", "tsv", "json", "xlsx"}:
        assert "iso3" in roles
    if expected in {"csv", "tsv", "geojson"}:
        assert report.geometry_kind == "points"


def test_probe_world_bank_json_finds_and_flattens_records():
    payload = [{"page": 1, "pages": 1, "per_page": 400, "total": 2}, [
        {"indicator": {"id": "AG.LND.PRCP.MM", "value": "Average precipitation"},
         "country": {"id": "AF", "value": "Afghanistan"},
         "countryiso3code": "AFG", "date": "2022", "value": 327,
         "obs_status": "", "decimal": 0},
        {"indicator": {"id": "AG.LND.PRCP.MM", "value": "Average precipitation"},
         "country": {"id": "AL", "value": "Albania"},
         "countryiso3code": "ALB", "date": "2022", "value": 1485,
         "tags": ["measured"]},
    ]]
    rows, format_name, geometries, warnings = probe.records(json.dumps(payload).encode())
    assert format_name == "json" and geometries == []
    assert len(rows) == 2 and warnings == ["records found at $[1]"]
    assert rows[0]["country.value"] == "Afghanistan"
    assert rows[0]["indicator.id"] == "AG.LND.PRCP.MM"
    assert rows[1]["tags"] == '["measured"]'
    assert "country" not in rows[0]


@pytest.mark.parametrize(("container", "path"), [
    ({"page": 1, "maxPages": 1, "items": [
        {"year": 2024, "coa_name": "Afghanistan", "coa_iso": "AFG", "refugees": 20866}
    ]}, "$.items"),
    ({"results": [{"id": 1}]}, "$.results"),
    ({"value": [{"id": 1}]}, "$.value"),
    ({"records": [{"id": 1}]}, "$.records"),
    ({"features": [{"id": 1}]}, "$.features"),
])
def test_probe_nested_json_record_shapes(container, path):
    report = probe.probe_bytes(json.dumps(container).encode())
    assert report.n_rows == 1
    assert report.warnings == [f"records found at {path}"]


def test_probe_chooses_largest_json_record_list_and_first_tie():
    payload = {"first": [{"chosen": 1}], "nested": {"larger": [
        {"other": 1}, {"other": 2}
    ]}, "tie": [{"wrong": 1}, {"wrong": 2}]}
    rows, _, _, warnings = probe.records(json.dumps(payload).encode())
    assert rows == [{"other": 1}, {"other": 2}]
    assert warnings == ["records found at $.nested.larger"]


def test_probe_accepts_an_empty_top_level_record_list():
    rows, format_name, geometries, warnings = probe.records(b"[]")
    assert (rows, format_name, geometries, warnings) == ([], "json", [], [])


def test_probe_zip_and_coverage_fixture(monkeypatch):
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as zipped:
        zipped.writestr("data.csv", "ISO3,value\nFRA,1\n")
    report = probe.probe_bytes(archive.getvalue())
    assert report.format == "csv" and "inside zip" in report.warnings[0]

    units = [
        {"id": "FRA", "keys": {"ISO3": "FRA"}, "name": "France",
         "properties": {"NAME_LONG": "French Republic"}},
        {"id": "DEU", "keys": {"ISO3": "DEU"}, "name": "Germany",
         "properties": {"FORMAL_EN": "Federal Republic of Germany"}},
    ]
    monkeypatch.setattr("mapsvc.cartography.natural_earth.load",
                        lambda *args, **kwargs: Boundaries(units=units, keys=("ISO3",)))
    assert probe.coverage(["FRA", "ESP"], "ISO3", "admin_0", None) == {
        "matched": 1, "expected": 2, "ratio": 0.5, "scope": None}
    names = probe.coverage(["French Republic"], "name", "admin_0", "world")
    assert names["matched"] == 1 and names["ratio"] == 0.5
