import types

import pytest

from mapsvc.discovery import htmltables, http, probe, providers
from mapsvc.discovery.store import Store

URL = "https://e.org/p"


def _rows(n=6):
    return "".join(
        f"<tr><td><a href='/x'>Place {i}</a>[{i}]</td>"
        f"<td><span style='display:none'>00{i}</span>{i * 1000:,}<sup class='reference'>[1]</sup></td>"
        f"<td>{i}.5 *</td></tr>" for i in range(1, n + 1))


WIKI = f"""<html><body>
<table class="navbox"><tr><th>Nav</th><th>x</th></tr>{_rows()}</table>
<table class="infobox"><tr><th>Info</th><th>y</th></tr>{_rows()}</table>
<table class="wikitable sortable">
<caption>Population (in thousands)[2]</caption>
<thead>
<tr><th rowspan="2">Place</th><th colspan="2">Population</th></tr>
<tr><th>2020</th><th>Density</th></tr>
</thead>
<tbody>
{_rows(5)}
<tr><td colspan="3">Section heading</td></tr>
<tr><td rowspan="2">Twin</td><td>1<br>2</td><td>x y &dagger;</td></tr>
<tr><td>3</td><td>4</td></tr>
<tr><td><span class="sortkey">zzz</span>Total</td><td>99</td><td>1</td></tr>
</tbody></table>
<table><tr><td>a</td><td>b</td></tr></table>
</body></html>"""


def test_wikipedia_like_table_is_parsed_and_layout_tables_skipped():
    found = htmltables.tables(WIKI)
    assert len(found) == 1                      # navbox, infobox and the 1-row table are skipped
    table = found[0]
    assert table.index == 0 and table.caption == "Population (in thousands)"
    assert table.headers == ["Place", "Population – 2020", "Population – Density"]
    first = table.rows[0]
    assert first == {"Place": "Place 1", "Population – 2020": "1,000",
                     "Population – Density": "1.5"}
    twin = [r for r in table.rows if r["Place"] == "Twin"]
    assert len(twin) == 2 and twin[0]["Population – 2020"] == "1 2"
    assert twin[0]["Population – Density"] == "x y"
    assert twin[1]["Population – 2020"] == "3"       # rowspan carried down
    assert all(r["Place"] != "Section heading" for r in table.rows)
    assert table.rows[-1]["Place"] == "Total"             # sortkey text removed
    assert table.n_rows == len(table.rows) == 8 and table.n_cols == 3
    joined = " | ".join(table.notes)
    assert "in thousands" in joined and "not rescaled" in joined
    assert "total" in joined.lower() and "section-heading" in joined


def test_duplicate_and_empty_headers_are_made_unique():
    html = ("<table><tr><th>A</th><th>A</th><th></th></tr>"
            + "".join(f"<tr><td>{i}</td><td>2</td><td>3</td></tr>" for i in range(5))
            + "</table>")
    assert htmltables.tables(html)[0].headers == ["A", "A_2", "column_3"]


def test_short_tables_are_not_data():
    html = ("<table><tr><th>A</th><th>B</th></tr>"
            + "".join("<tr><td>1</td><td>2</td></tr>" for _ in range(4)) + "</table>")
    assert htmltables.tables(html) == []


def test_split_table_fragment():
    assert htmltables.split_table_fragment("https://e.org/p#table=3") == ("https://e.org/p", 3)
    assert htmltables.split_table_fragment("https://e.org/p") == ("https://e.org/p", None)
    assert htmltables.split_table_fragment("https://e.org/p#x") == ("https://e.org/p#x", None)


def _serve(monkeypatch, html=WIKI):
    seen = []

    def get(url, **kwargs):
        seen.append((url, kwargs))
        return http.Response(200, {"content-type": "text/html"}, html.encode(), url)
    monkeypatch.setattr(http, "get", get)
    return seen


def test_probe_selects_the_table_named_by_the_fragment(monkeypatch):
    seen = _serve(monkeypatch)
    report = probe.probe(URL + "#table=0")
    assert seen[0][0] == URL and seen[0][1]["expect"] is None
    assert report.format == "html_table" and report.url == URL + "#table=0"
    assert report.n_rows == 8
    assert [c["name"] for c in report.columns][0] == "Place"
    assert any("in thousands" in w for w in report.warnings)
    rows, fmt, _g, warnings = probe.records(WIKI.encode(), table=0)
    assert fmt == "html_table" and len(rows) == 8 and warnings


def test_probe_rejects_a_missing_table_and_html_without_selector(monkeypatch):
    _serve(monkeypatch)
    with pytest.raises(probe.ProbeError, match="does not exist"):
        probe.probe(URL + "#table=4")
    with pytest.raises(probe.ProbeError):
        probe.records(b"%PDF-1.4 binary", table=0)


def test_fetch_lists_the_pages_data_tables(monkeypatch):
    big = "<table><tr><th>K</th><th>V</th></tr>" + "".join(
        f"<tr><td>k{i}</td><td>{i}</td></tr>" for i in range(30)) + "</table>"
    html = f"<html><head><title>T</title></head><body><p>PROSE</p>{WIKI}{big}</body></html>"
    _serve(monkeypatch, html)
    result = providers.fetch(URL)
    assert [t["index"] for t in result["tables"]] == [0, 1]
    assert result["tables"][1] == {"index": 1, "caption": "", "n_rows": 30, "n_cols": 2,
                                   "headers": ["K", "V"]}
    assert result["links"] == [URL + "#table=1", URL + "#table=0"]    # largest first
    assert "PROSE" not in str(result)


def test_a_discovered_dataset_reads_its_table(tmp_path, monkeypatch):
    from mapsvc.discovery import join
    from mapsvc.statistics import discovered
    monkeypatch.setenv("MAPSVC_DISCOVERED_DB", str(tmp_path / "d.sqlite"))
    Store().add_or_update({
        "id": "disc_tbl", "url": URL + "#table=0", "title": "Pop", "publisher": "Wiki",
        "licence": "CC BY-SA", "geometry_kind": "choropleth", "key_namespace": "name",
        "level": "admin_0", "status": "active", "format": "html_table", "units": "people",
        "roles": {"name": "Place", "value": "Population – 2020", "scale": 1000,
                  "filter": [{"column": "Place", "op": "ne", "value": "Total"}]}})
    seen = _serve(monkeypatch)
    fake = types.SimpleNamespace(resolver=lambda values: (lambda v: v and v.upper()))
    monkeypatch.setattr(join, "index", lambda *a, **k: fake)
    values = discovered.load("disc_tbl", "admin_0")
    assert seen[0][0] == URL and seen[0][1]["expect"] is None
    assert values.values["PLACE 2"] == 2000 * 1000 and "TOTAL" not in values.values


def test_a_web_layer_reads_the_selected_table(tmp_path, monkeypatch):
    from mapsvc.overlays import Window, web
    monkeypatch.setenv("MAPSVC_DISCOVERED_DB", str(tmp_path / "d.sqlite"))
    html = ("<table><tr><th>Name</th><th>Lon</th><th>Lat</th></tr>"
            + "".join(f"<tr><td>P{i}</td><td>{i}.0</td><td>{i + 10}.0</td></tr>"
                      for i in range(6)) + "</table>")
    Store().add_or_update({
        "id": "disc_pts", "url": URL + "#table=0", "title": "Pts", "publisher": "Wiki",
        "licence": "CC BY-SA", "geometry_kind": "points", "key_namespace": "coords",
        "level": "points", "status": "active", "format": "html_table",
        "roles": {"lon": "Lon", "lat": "Lat", "name": "Name"}})
    assert web.parse(URL + "#table=0") == URL + "#table=0"
    monkeypatch.setattr(http, "get", lambda url, **kw: http.Response(
        200, {"content-type": "text/html"}, html.encode(), url))
    monkeypatch.setattr(web.fetch, "path",
                        lambda *parts: tmp_path.joinpath(*parts), raising=False)
    payload = web.fetch_bytes(URL + "#table=0")
    layer = web.layer_from_bytes(payload, URL + "#table=0",
                                 Window(None, None, (-180.0, -90.0, 180.0, 90.0)))
    assert len(layer.get("features", layer.get("places", []))) == 6
    assert "Wiki" in layer["provenance"]["attribution"]
