"""Pure, offline tests for the reference-map checks."""

import csv
import json

import pytest

from evals.maps.run import (
    _class_balance_check,
    _footnote_consistency_check,
    _must_include_check,
    _svg_root,
    _truth_check,
    _vintage_check,
    evaluate_spot,
    parse_spot,
    recheck_one,
)


def _svg(body, label="Synthetic map, 2024"):
    return _svg_root(
        f'<svg xmlns="http://www.w3.org/2000/svg" aria-label="{label}">{body}</svg>'
    )


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


def test_must_include_matches_unit_ids_and_overlay_feature_names():
    root = _svg(
        '<path data-id="USA"/><g data-overlay="web"><circle data-fid="1"/></g>'
        '<text>Lutsk 220k</text>'
    )

    result = _must_include_check(
        {"must_include": ["USA", "Lutsk", "Kovel"]}, root,
    )

    assert result["status"] == "fail"
    assert result["found"] == ["USA", "Lutsk"]
    assert result["missing"] == ["Kovel"]


def test_vintage_flags_stale_and_mixed_stated_years(tmp_path):
    truth = tmp_path / "truth.csv"
    truth.write_text("key,value,year\nA,1,2024\nB,2,2019\n", encoding="utf-8")
    stale = _svg(
        '<text>Variable: Measure, 2020 [Source].</text><text>Projection: Equal Earth.</text>',
        "Measure, 2020",
    )
    mixed = _svg(
        '<text>Variable: Measure, latest year 2024 (A 2019) [Source].</text>'
        '<text>Projection: Equal Earth.</text>',
    )

    stale_result = _vintage_check({}, stale, truth)
    mixed_result = _vintage_check({}, mixed, truth)

    assert stale_result["status"] == "fail"
    assert stale_result["age_years"] == 4
    assert mixed_result["status"] == "fail"
    assert mixed_result["map_span_years"] == 5


def test_vintage_uses_end_of_a_period_instead_of_treating_it_as_mixed(tmp_path):
    truth = tmp_path / "truth.csv"
    truth.write_text("key,value,year\nA,1,2000-2023\n", encoding="utf-8")
    root = _svg(
        '<text>Variable: Population change, 2000–2023 [Source].</text>'
        '<text>Projection: Equal Earth.</text>',
        "Population change, 2000–2023",
    )

    result = _vintage_check({}, root, truth)

    assert result["status"] == "pass"
    assert result["map_years"] == [2023]


def test_class_balance_uses_legend_classes_and_fails_as_a_warning():
    root = _svg(
        '<g class="legend"><rect fill="#aaa"/><rect fill="#bbb"/>'
        '<rect fill="#ccc"/></g>'
        '<path data-id="A" data-value="1" fill="#aaa"/>'
        '<path data-id="B" data-value="2" fill="#aaa"/>'
        '<path data-id="C" data-value="3" fill="#aaa"/>'
        '<path data-id="D" data-value="4" fill="#bbb"/>'
        '<path data-id="E" data-value="5" fill="#ccc"/>'
    )

    result = _class_balance_check(root)

    assert result["status"] == "fail"
    assert result["severity"] == "warning"
    assert result["largest_class_fraction"] == pytest.approx(0.6)


def test_class_balance_allows_a_dominant_class_caused_by_ties():
    root = _svg(
        '<rect fill="#aaa"/><rect fill="#bbb"/>'
        '<path data-id="A" data-value="0" fill="#aaa"/>'
        '<path data-id="B" data-value="0" fill="#aaa"/>'
        '<path data-id="C" data-value="0" fill="#aaa"/>'
        '<path data-id="D" data-value="1" fill="#bbb"/>'
        '<path data-id="E" data-value="2" fill="#bbb"/>'
    )

    result = _class_balance_check(root)

    assert result["status"] == "pass"
    assert result["tied_values_exception"] is True


def test_footnote_consistency_counts_absent_truth_and_hatched_units(tmp_path):
    truth = tmp_path / "truth.csv"
    truth.write_text("key,value,year\nA,1,2024\nB,2,2024\nC,3,2024\n", encoding="utf-8")
    root = _svg(
        '<path data-id="A" data-value="1" fill="#aaa"/>'
        '<path data-id="B" fill="url(#nodata-hatch)"/>'
        '<text>1 feature excluded.</text>'
    )

    result = _footnote_consistency_check(root, truth)

    assert result["status"] == "fail"
    assert result["claimed_excluded"] == 1
    assert result["observed_excluded"] == 2
    assert result["absent_truth_keys"] == ["C"]
    assert result["hatched_ids"] == ["B"]


def test_recheck_uses_existing_artifacts_without_drawing(tmp_path, monkeypatch):
    case = tmp_path / "synthetic"
    case.mkdir()
    (case / "manifest.json").write_text(
        json.dumps({"level": "admin_0", "bbox": [-10, -10, 10, 10]}),
        encoding="utf-8",
    )
    (case / "map.svg").write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" aria-label="Measure, 2024">'
        '<path data-id="A" data-value="1" fill="#aaa"/>'
        '<text>Source: Test. Variable: Measure, 2024. Classification: none. '
        'Projection: Equal Earth. 0 features excluded.</text></svg>',
        encoding="utf-8",
    )
    (case / "truth.csv").write_text("key,value,year\nA,1,2024\n", encoding="utf-8")
    monkeypatch.setattr("evals.maps.run.REFS", tmp_path)

    result = recheck_one(
        {"id": "synthetic", "prompt": "test", "units": [1, 1]}, case,
    )

    assert result["passed"] is True
    assert result["rechecked"] is True
    assert json.loads((case / "auto.json").read_text())["checks"]["vintage"]["status"] == "pass"


def test_lint_flags_an_exclude_inside_its_contains_box():
    from evals.maps.run import lint_specs

    bad = {"id": "x", "contains": [[0, 0], [10, 10]], "excludes": [[5, 5]]}
    good = {"id": "y", "contains": [[0, 0], [10, 10]], "excludes": [[20, 5]]}
    assert len(lint_specs([bad, good])) == 1
