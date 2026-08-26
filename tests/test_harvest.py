import json

import pytest

from mapsvc import harvest as H
from mapsvc.manifest import validate
from tests.conftest import build_manifest

BOX = {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]]}


def feature(a3, continent="Europe", **props):
    base = {"ADM0_A3": a3, "CONTINENT": continent, "NAME": a3,
            "POP_EST": 1000, "POP_YEAR": 2019, "GDP_MD": 500, "GDP_YEAR": 2019,
            "POP_RANK": 5, "INCOME_GRP": "3. Upper middle income",
            "ECONOMY": "6. Developing region", "SUBREGION": "Western Europe"}
    base.update(props)
    return {"type": "Feature", "properties": base, "geometry": BOX}


@pytest.fixture
def offline(monkeypatch, tmp_path):
    """Point the cache at a temp dir and stub the fetch."""
    monkeypatch.setenv("MAPSVC_CACHE", str(tmp_path))

    def install(features):
        monkeypatch.setattr(H, "load_source", lambda: {"features": features})
    return install


def test_minus_99_and_zero_land_in_dropped_not_the_value_domain(offline):
    offline([
        feature("AAA", GDP_MD=100),
        feature("BBB", GDP_MD=-99),     # explicit no-data marker
        feature("CCC", GDP_MD=0),       # zero count is a placeholder row
        feature("DDD", GDP_MD=300),
    ])
    result = H.harvest(build_manifest(region="europe", variable_id="GDP_MD"))
    assert sorted(r["id"] for r in result.rows) == ["AAA", "DDD"]
    assert {d["id"]: d["reason"] for d in result.dropped} == {"BBB": "no_data", "CCC": "no_data"}
    values = [r["value"] for r in result.rows]
    assert -99 not in values and 0 not in values
    assert min(values) == 100.0


def test_a_sentinel_would_otherwise_be_shaded_as_the_lowest_real_value(offline):
    """Why the sentinel filter matters, not just that it fires.

    Measured against the real 10m file, -99 and 0 barely move an
    equal_interval domain -- world GDP already spans 10 to 21,433,226, so the
    skew comes from the United States, not from the sentinel. What the filter
    actually prevents is a lie: unfiltered, the Vatican's -99 becomes the
    darkest-or-palest real value on the map instead of being marked no-data.
    """
    from mapsvc import classify
    offline([feature(f"C{i:02d}", GDP_MD=v)
             for i, v in enumerate([100, 200, 300, 400, 500, -99])])
    result = H.harvest(build_manifest(region="europe", method="quantile", k=5))
    assert min(r["value"] for r in result.rows) == 100.0

    polluted = classify.build([100, 200, 300, 400, 500, -99], "count", "quantile", 5)
    clean = classify.build([100, 200, 300, 400, 500], "count", "quantile", 5)
    # Unfiltered, the sentinel occupies the bottom bin and shifts every real
    # country up a class.
    assert polluted.bin_of(-99) == 0
    assert polluted.bin_of(100) != clean.bin_of(100)


def test_zero_is_kept_for_ordinal_variables(offline):
    """A count of zero is a placeholder; a rank of zero could be real."""
    offline([feature("AAA", POP_RANK=0), feature("BBB", POP_RANK=5),
             feature("CCC", POP_RANK=-99)])
    result = H.harvest(build_manifest(region="europe", variable_id="POP_RANK", ramp="YlGnBu"))
    assert sorted(r["id"] for r in result.rows) == ["AAA", "BBB"]
    assert [d["id"] for d in result.dropped] == ["CCC"]


def test_dropped_rows_keep_their_geometry(offline):
    """`missing: "hatch"` has to draw the features it excludes."""
    offline([feature("AAA", GDP_MD=100), feature("BBB", GDP_MD=-99)])
    result = H.harvest(build_manifest(region="europe"))
    assert result.dropped[0]["geometry"] == BOX


def test_region_matches_continent_then_country_code(offline):
    offline([feature("FRA", "Europe"), feature("KEN", "Africa"), feature("DEU", "Europe")])
    assert len(H.harvest(build_manifest(region="europe")).rows) == 2
    assert len(H.harvest(build_manifest(region="Africa")).rows) == 1
    assert [r["id"] for r in H.harvest(build_manifest(region="KEN")).rows] == ["KEN"]
    assert len(H.harvest(build_manifest(region="world")).rows) == 3


def test_unknown_region_names_the_region_field(offline):
    offline([feature("FRA", "Europe")])
    with pytest.raises(H.HarvestError) as excinfo:
        H.harvest(build_manifest(region="atlantis"))
    assert excinfo.value.field == "region"


def test_normalize_divides_and_drops_unusable_divisors(offline):
    offline([
        feature("AAA", GDP_MD=1000, POP_EST=100),
        feature("BBB", GDP_MD=1000, POP_EST=0),      # zero count -> no divisor
        feature("CCC", GDP_MD=1000, POP_EST=-99),
    ])
    result = H.harvest(build_manifest(region="europe", normalize="POP_EST"))
    assert [(r["id"], r["value"]) for r in result.rows] == [("AAA", 10.0)]
    assert {d["reason"] for d in result.dropped} == {"no_data_normalize"}


def test_year_is_a_single_value_or_a_range(offline):
    offline([feature("AAA", GDP_YEAR=2019), feature("BBB", GDP_YEAR=2019)])
    assert H.harvest(build_manifest(region="europe")).provenance["year"] == "2019"
    offline([feature("AAA", GDP_YEAR=1999), feature("BBB", GDP_YEAR=2019)])
    assert H.harvest(build_manifest(region="AAA")).provenance["year"] == "1999"


def test_variables_without_a_year_column_report_none(offline):
    offline([feature("AAA")])
    result = H.harvest(build_manifest(region="europe", variable_id="SUBREGION", ramp="Set2"))
    assert result.provenance["year"] is None


def test_cache_key_ignores_ramp_and_classify():
    a = validate({"region": "europe", "level": "admin_0",
                  "variable": {"source": "natural_earth", "id": "GDP_MD"},
                  "normalize": "POP_EST", "classify": {"method": "quantile", "k": 5},
                  "ramp": "YlGnBu"})
    b = validate({"region": "europe", "level": "admin_0",
                  "variable": {"source": "natural_earth", "id": "GDP_MD"},
                  "normalize": "POP_EST", "classify": {"method": "jenks", "k": 9},
                  "ramp": "Blues"})
    assert H.cache_key(a) == H.cache_key(b)


def test_cache_key_changes_with_data_relevant_fields():
    def key(**over):
        return H.cache_key(build_manifest(**over))
    base = key()
    assert base != key(region="africa")
    assert base != key(variable_id="POP_EST")
    assert base != key(normalize="POP_EST")
    assert base == key(ramp="Blues", method="jenks", k=9, projection="mercator")


def test_second_harvest_reads_the_cache_and_matches(offline, monkeypatch):
    offline([feature("AAA", GDP_MD=100), feature("BBB", GDP_MD=-99)])
    manifest = build_manifest(region="europe")
    first = H.harvest(manifest)

    def explode():
        raise AssertionError("second harvest must not re-read the source")
    monkeypatch.setattr(H, "load_source", explode)

    second = H.harvest(manifest)
    assert (second.rows, second.provenance, second.dropped) == \
           (first.rows, first.provenance, first.dropped)


def test_rows_and_dropped_come_back_in_a_stable_order(offline):
    offline([feature(a3, GDP_MD=v) for a3, v in
             [("DDD", 4), ("AAA", 1), ("CCC", -99), ("BBB", 2)]])
    result = H.harvest(build_manifest(region="europe"))
    assert [r["id"] for r in result.rows] == ["AAA", "BBB", "DDD"]
    assert [d["id"] for d in result.dropped] == ["CCC"]
