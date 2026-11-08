"""Pure URL-adapter and provider short-circuit tests."""

import json

import pytest

from mapsvc.discovery import adapters, http, providers


@pytest.mark.parametrize(("landing", "expected"), [
    ("https://data.worldbank.org/indicator/AG.LND.PRCP.MM?view=map",
     "https://api.worldbank.org/v2/country/all/indicator/AG.LND.PRCP.MM?format=json&per_page=20000&mrnev=1"),
    ("https://api.worldbank.org/v2/country/all/indicator/SP.POP.TOTL?per_page=50",
     "https://api.worldbank.org/v2/country/all/indicator/SP.POP.TOTL?per_page=50&format=json"),
    ("https://ourworldindata.org/grapher/life-expectancy?tab=chart",
     "https://ourworldindata.org/grapher/life-expectancy.csv?tab=chart"),
    ("https://github.com/acme/data/blob/main/tables/rain.csv",
     "https://raw.githubusercontent.com/acme/data/main/tables/rain.csv"),
    ("https://docs.google.com/spreadsheets/d/abc123/edit?usp=sharing&gid=42",
     "https://docs.google.com/spreadsheets/d/abc123/export?format=csv&gid=42"),
    ("https://docs.google.com/spreadsheets/d/abc123/edit#gid=7",
     "https://docs.google.com/spreadsheets/d/abc123/export?format=csv&gid=7"),
    ("https://data.city.gov/d/abcd-1234",
     "https://data.city.gov/resource/abcd-1234.csv?$limit=50000"),
    ("https://data.city.gov/Health/Hospitals/wxyz-9876",
     "https://data.city.gov/resource/wxyz-9876.csv?$limit=50000"),
    ("https://data.humdata.org/dataset/afghanistan-population",
     "https://data.humdata.org/api/3/action/package_show?id=afghanistan-population"),
    ("https://data.gov.ua/dataset/weather/",
     "https://data.gov.ua/api/3/action/package_show?id=weather"),
])
def test_known_landing_pages(landing, expected):
    assert adapters.data_urls(landing) == [expected]


@pytest.mark.parametrize("url", [
    "https://example.org/indicator/AG.LND.PRCP.MM",
    "https://github.com/acme/data/tree/main",
    "https://data.humdata.org/organization/acme",
    "file:///tmp/data.csv",
])
def test_non_matches(url):
    assert adapters.data_urls(url) == []


def test_provider_short_circuits_landing_page(monkeypatch):
    monkeypatch.setattr(http, "get", lambda *args, **kwargs: pytest.fail("unexpected I/O"))
    result = providers.fetch("https://ourworldindata.org/grapher/life-expectancy")
    assert result == {"kind": "page", "title": "", "links": [
        "https://ourworldindata.org/grapher/life-expectancy.csv"
    ]}


def test_provider_fetches_ckan_metadata_and_filters_resources(monkeypatch):
    package = {"result": {"title": "A" * 170, "resources": [
        {"format": "CSV", "url": "https://files.example/data"},
        {"format": "GeoJSON", "url": "https://files.example/map"},
        {"format": "PDF", "url": "https://files.example/report.pdf"},
    ]}}
    calls = []

    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        return http.Response(200, {"Content-Type": "application/json"},
                             json.dumps(package).encode(), url)

    monkeypatch.setattr(http, "get", fake_get)
    result = providers.fetch("https://data.humdata.org/dataset/example")
    assert calls == [("https://data.humdata.org/api/3/action/package_show?id=example",
                      {"expect": "json"})]
    assert result["links"] == ["https://files.example/data", "https://files.example/map"]
    assert len(result["title"]) == 160 and result["title"].endswith("…")
