"""Pure, offline tests for the reference-map checks."""

import csv

import pytest

from evals.maps.run import _truth_check, evaluate_spot, parse_spot


@pytest.mark.parametrize(
    ("expression", "kind"),
    [
        ("LUX=126000±15%", "approx"),
        ("UA-30>UA-14", "comparison"),
        ("BGR<-10", "comparison"),
        ("06085>150000", "comparison"),
    ],
)
def test_spot_parser_supports_the_declared_language(expression, kind):
    assert parse_spot(expression)["kind"] == kind


def test_spot_evaluator_checks_tolerances_keys_and_literals():
    values = {"LUX": "120000", "UA-30": "2952300", "UA-14": "1900000",
              "BGR": "-12", "06085": "155000"}
    assert evaluate_spot("LUX=126000±15%", values)["status"] == "pass"
    assert evaluate_spot("UA-30>UA-14", values)["status"] == "pass"
    assert evaluate_spot("BGR<-10", values)["status"] == "pass"
    assert evaluate_spot("06085>150000", values)["status"] == "pass"
    assert evaluate_spot("06085<60000", values)["status"] == "fail"


def test_unsupported_or_missing_spots_are_explicitly_unevaluable():
    unsupported = evaluate_spot("total_km~2800", {})
    missing = evaluate_spot("TX>CA", {"TX": "1"})
    assert unsupported["status"] == "unevaluable"
    assert "supported forms" in unsupported["reason"]
    assert missing["status"] == "unevaluable"
    assert "CA" in missing["reason"]


def test_near_checks_measure_distance_to_overlay_geometry():
    geometry = {"points": [(95.94, 22.01)],
                "lines": [[(4.70, 45.70), (4.75, 45.90)]]}
    assert evaluate_spot("event_near=96.0,22.0", geometry=geometry, values={})["status"] == "pass"
    assert evaluate_spot("event_near=160.0,52.5", geometry=geometry, values={})["status"] == "fail"
    near = evaluate_spot("line_near=4.83,45.76", geometry=geometry, values={})
    assert near["status"] == "pass" and near["nearest_km"] < 10
    assert evaluate_spot("line_near=4.83,45.76", {})["status"] == "unevaluable"


def test_truth_check_matches_short_keys_to_iso_3166_2_ids(tmp_path):
    truth = tmp_path / "truth.csv"
    with truth.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerows((("key", "value"), ("AK", "12.5"), ("DC", "3.5")))

    result = _truth_check(
        truth,
        {"US-AK", "US-DC"},
        {"US-AK": "12.5", "US-DC": "3.5"},
    )

    assert result["present_keys"] == 2
    assert result["matching_value_keys"] == 2
    assert result["fraction_keys_present"] == 1
    assert result["median_abs_pct_error"] == 0


def test_truth_check_matches_prefixed_truth_to_short_rendered_ids(tmp_path):
    truth = tmp_path / "truth.csv"
    truth.write_text("key,value\nUA-30,100\nUA-32,200\n", encoding="utf-8")

    result = _truth_check(truth, {"30", "32"}, {"30": "100", "32": "200"})

    assert result["present_keys"] == 2
    assert result["matching_value_keys"] == 2


def test_truth_check_does_not_guess_ambiguous_short_ids(tmp_path):
    truth = tmp_path / "truth.csv"
    truth.write_text("key,value\nCA,100\n", encoding="utf-8")

    result = _truth_check(
        truth,
        {"US-CA", "ES-CA"},
        {"US-CA": "100", "ES-CA": "100"},
    )

    assert result["present_keys"] == 0
    assert result["matching_value_keys"] == 0


def test_truth_check_does_not_equate_different_country_prefixes(tmp_path):
    truth = tmp_path / "truth.csv"
    truth.write_text("key,value\nUA-30,100\n", encoding="utf-8")

    result = _truth_check(truth, {"DE-30"}, {"DE-30": "100"})

    assert result["present_keys"] == 0
