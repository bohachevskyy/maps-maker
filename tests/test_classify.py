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


def test_diverging_breaks_put_zero_on_a_class_edge():
    values = [-30.0, -12.0, -4.0, -1.0, 2.0, 5.0, 9.0, 40.0, 120.0, 210.0]
    scheme = classify.diverging(values, "quantile", 5)
    assert scheme.zero_at is not None
    assert scheme.breaks[scheme.zero_at] == 0.0
    # Every negative value falls below the zero edge and every positive above.
    for v in values:
        assert (scheme.bin_of(v) < scheme.zero_at) == (v < 0)


def test_diverging_classifies_each_side_on_its_own():
    """One +200% outlier must not squash every growing unit into one class."""
    values = [-20.0, -10.0, -5.0, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 200.0]
    scheme = classify.diverging(values, "quantile", 5)
    bins = {scheme.bin_of(v) for v in values if v > 0}
    assert len(bins) == scheme.k - scheme.zero_at > 1


def test_diverging_never_asks_a_ramp_for_more_than_it_holds():
    values = [float(v) for v in range(-50, 51) if v]
    scheme = classify.diverging(values, "quantile", 9)
    assert scheme.zero_at <= 4 and scheme.k - scheme.zero_at <= 4


def test_diverging_requires_ten_percent_on_each_side():
    assert classify.substantially_diverging([-2, 1, 2, 3, 4, 5, 6, 7, 8, 9])
    assert not classify.substantially_diverging([-2] + list(range(1, 20)))
    assert not classify.substantially_diverging([0, 1, 2, 3])


# ------------------------------------------------------------ round breaks ---

def _counts(scheme, values):
    out = [0] * scheme.k
    for v in values:
        out[scheme.bin_of(v)] += 1
    return out


def test_two_orders_of_magnitude_get_log_spaced_round_breaks():
    """GDP per capita, $1k to $140k: quantiles put the US and the Gulf in one
    class. Round 1-2-5 breaks separate them, and the end classes are open."""
    values = [1066.0, 1500.0, 2400.0, 3100.0, 4800.0, 7000.0, 9000.0, 14000.0,
              19000.0, 26000.0, 42000.0, 55000.0, 77000.0, 95000.0, 139592.0]
    scheme = classify.nice(values)
    assert scheme.breaks[1:-1] == [2000.0, 5000.0, 10000.0, 20000.0, 50000.0, 100000.0]
    assert scheme.open_ends and "log" in scheme.scheme
    assert scheme.labels("$") == ["< $2k", "$2k–5k", "$5k–10k", "$10k–20k",
                                  "$20k–50k", "$50k–100k", "≥ $100k"]
    assert all(_counts(scheme, values))


def test_diverging_data_get_round_breaks_either_side_of_zero():
    shrinking = [-34.0, -28.0, -24.0, -21.0, -18.0, -15.0, -13.0, -12.0, -11.0,
                 -9.0, -8.0, -6.0, -5.5, -3.0, -2.0, -1.0]
    growing = [1.0, 4.0, 8.0, 12.0, 15.0, 18.0, 22.0, 26.0, 30.0, 35.0, 41.0, 48.0,
               55.0, 63.0, 70.0, 80.0, 95.0, 110.0, 127.0, 160.0, 205.0, 362.0] * 3
    scheme = classify.nice(shrinking + growing)
    assert scheme.breaks[scheme.zero_at] == 0.0
    assert 4 <= scheme.zero_at <= 5
    assert 4 <= scheme.k - scheme.zero_at <= 5
    assert 200.0 in scheme.breaks
    assert scheme.breaks[scheme.zero_at] == 0.0
    labels = scheme.labels(suffix="%")
    assert labels[0] == "< −30%" and labels[-1] == "≥ +200%"
    for v in shrinking + growing:
        assert (scheme.bin_of(v) < scheme.zero_at) == (v < 0)


def test_a_share_spread_across_0_100_gets_the_fixed_scale():
    values = [4.8, 11.0, 17.0, 26.0, 33.0, 47.0, 56.0, 71.0, 84.0, 99.0, 100.0]
    scheme = classify.nice(values, percent=True)
    assert scheme.breaks == [float(v) for v in range(0, 81, 10)] + [100.0]
    assert not scheme.open_ends
    assert scheme.labels(suffix="%")[0] == "0–10%"
    assert scheme.labels(suffix="%")[-1] == "80–100%"
    assert scheme.bin_of(100.0) == 8


def test_a_share_in_a_narrow_band_is_not_forced_onto_0_100():
    values = [61.0, 63.0, 64.5, 66.0, 68.0, 70.0, 71.0, 73.0, 75.0, 78.0]
    scheme = classify.nice(values, percent=True)
    assert scheme.breaks[0] == 61.0 and scheme.open_ends


def test_otherwise_natural_breaks_are_rounded_and_no_class_is_empty():
    # Distillate per head: three states far above the rest.
    values = [0.97, 2.3, 2.4, 2.6, 2.9, 3.1, 3.2, 3.4, 3.5, 3.7, 3.9, 4.1, 4.3, 4.4,
              4.6, 4.9, 5.3, 5.8, 6.2, 6.5, 7.0, 7.4, 7.8, 8.3, 9.2, 19.3, 19.6, 23.2]
    scheme = classify.nice(values)
    interior = scheme.breaks[1:-1]
    assert all(classify.round_sig(e) == e for e in interior)
    counts = _counts(scheme, values)
    assert all(counts)
    # The outliers share a class rather than one getting a colour to itself.
    assert counts[-1] >= classify.END_CLASS_MIN
    assert scheme.bin_of(19.3) == scheme.bin_of(23.2) == scheme.k - 1


def test_round_breaks_are_deterministic_and_bounded():
    values = [float(v * v) for v in range(1, 60)]
    first, second = classify.nice(values), classify.nice(list(reversed(values)))
    assert first == second
    assert classify.MIN_CLASSES - 2 <= first.k <= classify.MAX_CLASSES


def test_few_distinct_values_get_a_class_each():
    scheme = classify.nice([1.0, 2.0, 2.0, 3.0])
    assert scheme.k == 3


def test_long_tailed_election_margin_uses_steps_for_both_extremes():
    values = [-84, -32, -29, -25, -20, -16, -12, -8, -5, -3, -1,
              1, 2, 4, 6, 9, 12, 15, 20, 23, 30, 36, 46]
    scheme = classify.nice(values)
    assert 7 <= scheme.k <= 10
    assert scheme.bin_of(30) < scheme.bin_of(46)
    assert scheme.bin_of(-84) < scheme.bin_of(-32)


def test_small_diverging_set_still_separates_supported_steps():
    values = [-1.8, -1.7, 3.1, 3.8, 4.6, 4.8, 5.0, 5.0, 5.2, 5.8, 6.0, 7.4, 12.1]
    scheme = classify.nice(values)
    assert scheme.zero_at == 2
    assert scheme.k >= 6
    assert scheme.bin_of(4.8) != scheme.bin_of(5.8)


def test_nonzero_anchor_is_a_diverging_edge_without_signed_labels():
    values = [45, 62, 78, 91, 98, 104, 112, 130, 170, 260]
    scheme = classify.anchored(values, 100, "EU27 = 100")
    assert scheme.zero_at is None
    assert scheme.center_at is not None
    assert scheme.breaks[scheme.center_at] == 100
    assert all("+" not in label and "−" not in label for label in scheme.labels())


def test_nice_label_abbreviates():
    assert classify.nice_label(2000.0) == "2k"
    assert classify.nice_label(1_500_000.0) == "1.5M"
    assert classify.nice_label(-10.0) == "−10"
    assert classify.nice_label(20.0, signed=True) == "+20"
    assert classify.nice_label(0.25) == "0.25"
    assert classify.nice_label(130000.0) == "130k"
