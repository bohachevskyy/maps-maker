"""Pure, offline tests for the reference-map spot-check language."""

import pytest

from evals.maps.run import evaluate_spot, parse_spot


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
    unsupported = evaluate_spot("line_near=4.83,45.76", {})
    missing = evaluate_spot("TX>CA", {"TX": "1"})
    assert unsupported["status"] == "unevaluable"
    assert "supported forms" in unsupported["reason"]
    assert missing["status"] == "unevaluable"
    assert "CA" in missing["reason"]
