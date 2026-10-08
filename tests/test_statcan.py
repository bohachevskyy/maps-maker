"""Statistics Canada census populations, offline from a recorded table slice."""

import io
import zipfile

import pytest

from mapsvc import derive
from mapsvc.manifest import validate
from mapsvc.statistics import StatisticsError, statcan

HEADER = ('"REF_DATE","GEO","DGUID","Coordinate",'
          '"Population and dwelling counts (11): Population, 2021 [1]","Symbols",'
          '"Population and dwelling counts (11): Population, 2016 [2]","Symbols"\n')
COUNTS = {
    "Newfoundland and Labrador": (510550, 519716),
    "Prince Edward Island": (154331, 142907), "Nova Scotia": (969383, 923598),
    "New Brunswick": (775610, 747101), "Quebec": (8501833, 8164361),
    "Ontario": (14223942, 13448494), "Manitoba": (1342153, 1278365),
    "Saskatchewan": (1132505, 1098352), "Alberta": (4262635, 4067175),
    "British Columbia": (5000879, 4648055), "Yukon": (40232, 35874),
    "Northwest Territories": (41070, 41786), "Nunavut": (36858, 35944),
}


def _archive() -> bytes:
    rows = [HEADER]
    for index, (name, (new, old)) in enumerate(COUNTS.items(), start=2):
        rows.append(f'"2021","{name}","code","{index}","{new}",,"{old}",,\n')
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(statcan.TABLE, "".join(rows))
    return buffer.getvalue()


@pytest.fixture
def census(monkeypatch, tmp_path):
    monkeypatch.setenv("MAPSVC_CACHE", str(tmp_path))
    monkeypatch.setattr(statcan.fetch, "get", lambda *args, **kwargs: _archive())
    statcan._rows.cache_clear()
    yield
    statcan._rows.cache_clear()


def test_statcan_has_all_13_units_and_both_census_years(census):
    latest = statcan.load("population", "admin_1", "world")
    old = statcan.load("population", "admin_1", "world", year=2016)
    assert len(latest.values) == len(old.values) == 13
    assert latest.values["CA-NL"] == 510550
    assert old.values["CA-ON"] == 13448494
    assert latest.provenance["license"] == "Open Government Licence - Canada"


def test_canadian_population_change_matches_the_published_table(census):
    manifest = validate({
        "within": ["CAN"], "level": "admin_1",
        "basemap": {"source": "natural_earth"},
        "variable": {"source": "statcan", "id": "population",
                     "change": {"from": 2016, "to": 2021}},
    })
    values = derive.values(manifest)
    assert values.values["CA-NL"] == pytest.approx(-1.7636555)
    assert values.values["CA-YT"] == pytest.approx(12.1480738)
    assert values.values["CA-ON"] == pytest.approx(5.7660583)


def test_statcan_rejects_non_zip_without_caching(monkeypatch, tmp_path):
    monkeypatch.setenv("MAPSVC_CACHE", str(tmp_path))
    monkeypatch.setattr(statcan.fetch, "get", lambda *args, **kwargs: b"<html>denied</html>")
    with pytest.raises(StatisticsError, match="not return"):
        statcan._data()
    assert not (tmp_path / "statcan" / statcan.FILENAME).exists()
