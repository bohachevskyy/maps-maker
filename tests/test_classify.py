import pytest

from mapsvc import classify


def test_quantile_k5_over_20_values_gives_five_bins_of_four():
    values = list(range(20))
    scheme = classify.build(values, "count", "quantile", 5)
    counts = [0] * 5
    for v in values:
        counts[scheme.bin_of(v)] += 1
    assert counts == [4, 4, 4, 4, 4]
    assert len(scheme.breaks) == 6


def test_equal_interval_edges_are_evenly_spaced():
    breaks = classify.equal_interval([0.0, 10.0], 5)
    steps = [round(breaks[i + 1] - breaks[i], 9) for i in range(5)]
    assert steps == [2.0] * 5


def test_jenks_finds_natural_gaps_that_equal_interval_misses():
    values = [1, 2, 3, 4, 5, 100, 101, 102, 500, 501]
    natural = classify.build(values, "count", "jenks", 3)
    even = classify.build(values, "count", "equal_interval", 3)
    assert [natural.bin_of(v) for v in values] == [0, 0, 0, 0, 0, 1, 1, 1, 2, 2]
    # The outlier drags equal_interval's bins apart and leaves the middle empty.
    assert 1 not in {even.bin_of(v) for v in values}


def test_every_value_lands_in_a_bin():
    values = [3.0, 3.0, 7.5, 91.2, 91.2, 400.0]
    for method in ("quantile", "equal_interval", "jenks"):
        scheme = classify.build(values, "count", method, 4)
        assert all(0 <= scheme.bin_of(v) < scheme.k for v in values)


def test_ordinal_sorts_on_numeric_prefix():
    values = ["4. Lower middle income", "1. High income: OECD", "5. Low income",
              "3. Upper middle income", "2. High income: nonOECD"]
    scheme = classify.build(values, "ordinal", "quantile", 5)
    assert [c.split(".")[0] for c in scheme.categories] == ["1", "2", "3", "4", "5"]


def test_ordinal_prefix_sorts_numerically_not_lexically():
    # Natural Earth's own INCOME_GRP and ECONOMY prefixes are all single digits,
    # so alphabetical order happens to agree with them. It stops agreeing the
    # moment a prefix reaches two digits, which is what the rank key guards.
    values = ["10. Tenth", "9. Ninth", "2. Second"]
    scheme = classify.build(values, "ordinal", "quantile", 3)
    assert scheme.categories == ["2. Second", "9. Ninth", "10. Tenth"]
    assert sorted(values) == ["10. Tenth", "2. Second", "9. Ninth"]


def test_ordinal_ignores_the_prefix_text_when_ranking():
    # "Least developed" sorts before "Developing" alphabetically; the prefix wins.
    values = ["7. Least developed region", "6. Developing region"]
    scheme = classify.build(values, "ordinal", "quantile", 2)
    assert scheme.categories == ["6. Developing region", "7. Least developed region"]


def test_nominal_gets_one_bin_per_category_ignoring_k():
    values = ["Western Europe", "Eastern Europe", "Northern Europe", "Eastern Europe"]
    scheme = classify.build(values, "nominal", "quantile", 9)
    assert scheme.kind == "categorical"
    assert scheme.k == 3


def test_k_is_clamped_to_the_number_of_distinct_values():
    scheme = classify.build([1.0, 5.0, 9.0], "count", "quantile", 9)
    assert scheme.k == 3


def test_empty_domain_is_an_error():
    with pytest.raises(ValueError):
        classify.build([], "count", "quantile", 5)
